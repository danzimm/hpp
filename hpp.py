#!/usr/bin/env python3

import argparse
import copy
import os
import re
import shutil
import string
import subprocess
import sys
from threading import Thread
import time
import traceback

from bs4 import BeautifulSoup
from bs4.element import Tag
from http.server import test, SimpleHTTPRequestHandler

def ensure_parent(p):
    parent = os.path.dirname(p)
    if not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def attr_has_prefix(prefix):
    return lambda t: any(k.startswith(prefix) for k in t.attrs.keys())

def warn(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    print("W:", *args, **kwargs)

class SafeFormatDict(dict):
    def __init__(self, *args, **kwargs):
        self.didLookup = False
        super().__init__(*args, **kwargs)

    def __missing__(self, key):
        return key

    def __getitem__(self, *args, **kwargs):
        self.didLookup = True
        return super().__getitem__(*args, **kwargs)

class DepsMap:
    def __init__(self):
        self.template_to_files = {}
        self.file_to_templates = {}

    def __setitem__(self, file, templates):
        if file in self.file_to_templates:
            for template in self.file_to_templates[file]:
                self.template_to_files[template].remove(file)

        self.file_to_templates[file] = set(templates)
        for template in templates:
            self.template_to_files.setdefault(template, set()).add(file)

        print(f"  ++ {file} -> {templates}")

    def getDepsOfTemplate(self, template):
        return self.template_to_files.get(template, set())

    def removeTemplate(self, template):
        files = self.template_to_files.pop(template, set())
        for file in files:
            self.file_to_templates[file].remove(template)
        return files

class AutoReloader:
    def __init__(self, enabled, port, deps_map):
        self.enabled = enabled
        self.port = port
        self.deps_map = deps_map

    @property
    def baseUrl(self):
        return f"http://127.0.0.1:{self.port}"

    def viewingLiveSite(self, current_url=None):
        if current_url is None:
            current_url = self.getCurrentUrl()
        return current_url.startswith(self.baseUrl)

    def reloadIfNecessary(self, paths):
        if not self.enabled:
            return

        if len(paths) == 1:
            self(next(iter(paths)))
            return

        current_url = self.getCurrentUrl()
        if not self.viewingLiveSite(current_url):
            return
        current_url_path = current_url[len(self.baseUrl):]
        if current_url_path.startswith("/"):
            current_url_path = current_url_path[1:]
        if current_url_path.endswith("/"):
            current_url_path = current_url_path + "index.html"
        if current_url_path in paths:
            self.reload()

    def __call__(self, path):
        if not self.enabled:
            return

        if path[0] != "/":
            path = "/" + path
        head, tail = os.path.split(path)
        if tail == "index.html":
            self.open(head)
        else:
            _, ext = os.path.splitext(tail)
            if ext[1:] in ("svg", "css", "ico"):
                self.reload()

    def openAfterDelay(self, delay):
        time.sleep(delay)
        if not self.reload():
            self.open("/")

    def open(self, url_path):
        if self.viewingLiveSite():
            self.runAppleScript(f"""tell application "Safari"
set docUrl to "http://127.0.0.1:{self.port}{url_path}"
set URL of document 1 to docURL
end tell""")
        else:
            self.runAppleScript(f"""tell application "Safari"
tell window 1
set newTab to (make new tab)
set URL of newTab to "http://127.0.0.1:{self.port}{url_path}"
set current tab to newTab
end tell
end tell
""")

    def reload(self):
        if not self.viewingLiveSite():
            return False
        self.runAppleScript("""tell application "Safari"
set docUrl to URL of document 1
set URL of document 1 to docURL
end tell""")
        return True

    def getCurrentUrl(self):
        return self.runAppleScript("""on run
  tell application "Safari"
    return URL of document 1
  end tell
end run""", return_stdout=True).strip()

    def runAppleScript(self, script, return_stdout=False, open_window_on_fail=True):
        if not self.enabled:
            return ""
        try:
            cp = subprocess.run(["osascript", "-e", script], check=True, capture_output=return_stdout, text=True if return_stdout else None)
            return cp.stdout if return_stdout else ""
        except subprocess.CalledProcessError as exc:
            warn(f"Failed to run {script}: {exc}")
            if open_window_on_fail:
                self.runAppleScript("""tell application "Safari"
if (count documents) is 0 then make new document
end tell""", open_window_on_fail=False)
                return self.runAppleScript(script, return_stdout=return_stdout, open_window_on_fail=False)
            else:
                return ""

class DepNode:
    def __init__(self, name, parent = None, **kwargs):
        self.parent = parent
        self.name = name
        self.args = dict(kwargs)
        self.children = []

    def addChild(self, node):
        node.parent = self
        self.children.append(node)

class CircularDepException(Exception):
    def __init__(self, deps):
        self.deps = deps

    def __str__(self):
        return f"Found circular dependency for the following deps: {repr(self.deps)}"

    def __repr__(self):
        return f"CircularDepException({repr(self.deps)})"

def flatten_deps(key, deps, depset=None):
    if depset is None:
        depset = set()

    direct_deps = deps.get(key, set())
    circular_deps = direct_deps & depset
    if len(circular_deps) != 0:
        raise CircularDepException(circular_deps)
    depset |= direct_deps
    for dd in direct_deps:
        flatten_deps(dd, deps, depset)

    return depset

def field_name_root(field_name):
    return field_name.split(".", 1)[0].split("[", 1)[0]

def is_truthy(value):
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "false", "0", "no")

def expression_value(token, args):
    token = token.strip()
    if (
        (token.startswith('"') and token.endswith('"')) or
        (token.startswith("'") and token.endswith("'"))
    ):
        return token[1:-1]
    return args.get(token, "")

def eval_hpp_expr(expr, args, relpath):
    expr = expr.strip()
    if not expr:
        warn(f"Empty conditional expression in {relpath}, treating as false")
        return False

    if expr.startswith("!"):
        name = expr[1:].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            warn(f"Invalid conditional expression '{expr}' in {relpath}, treating as false")
            return False
        return not is_truthy(args.get(name, ""))

    comparison = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_-]*|'[^']*'|\"[^\"]*\")\s*(==|!=)\s*([A-Za-z_][A-Za-z0-9_-]*|'[^']*'|\"[^\"]*\")",
        expr,
    )
    if comparison is not None:
        lhs, op, rhs = comparison.groups()
        is_equal = expression_value(lhs, args) == expression_value(rhs, args)
        return is_equal if op == "==" else not is_equal

    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", expr):
        return is_truthy(args.get(expr, ""))

    warn(f"Invalid conditional expression '{expr}' in {relpath}, treating as false")
    return False

def format_hpp_value(value, args):
    new_value = format_hpp_text(value, args)
    return value if new_value is None else new_value

def build_hpp_args(hpp, inherited_args=None):
    args = SafeFormatDict(inherited_args or {})
    inherited_scope = SafeFormatDict(args)
    for key, value in hpp.attrs.items():
        args[key] = format_hpp_value(value, inherited_scope)
    return args

def add_class(elem, class_name):
    existing = elem.get("class", [])
    if isinstance(existing, str):
        classes = existing.split()
    else:
        classes = list(existing)
    for part in class_name.split():
        if part and part not in classes:
            classes.append(part)
    if classes:
        elem["class"] = classes

def iter_tags(elem):
    if isinstance(elem, Tag):
        yield elem
    yield from elem.find_all(True)

def iter_hpp_tags(elem):
    if isinstance(elem, Tag) and elem.name == "hpp":
        yield elem
    yield from elem.find_all("hpp")

def format_hpp_text(format_str, args):
    try:
        parsed_format = list(string.Formatter().parse(format_str))
    except ValueError:
        return None

    if not any(field_name for _, field_name, _, _ in parsed_format):
        return None

    text_parts = []
    for literal_text, field_name, default_value, _ in parsed_format:
        text_parts.append(literal_text)
        if field_name is None:
            continue
        arg_name = field_name_root(field_name)
        text_parts.append(args[arg_name] if arg_name in args else default_value)
    return "".join(text_parts)

def apply_element_conditionals(soup, args, relpath):
    for elem in [tag for tag in list(iter_tags(soup)) if "hpp-if" in tag.attrs]:
        keep = eval_hpp_expr(elem["hpp-if"], args, relpath)
        if "hpp-unless" in elem.attrs:
            warn(f"Both hpp-if and hpp-unless specified in {relpath}")
            keep = keep and not eval_hpp_expr(elem["hpp-unless"], args, relpath)
        if not keep:
            elem.decompose()
        else:
            del elem["hpp-if"]
            if "hpp-unless" in elem.attrs:
                del elem["hpp-unless"]

    for elem in [tag for tag in list(iter_tags(soup)) if "hpp-unless" in tag.attrs]:
        if eval_hpp_expr(elem["hpp-unless"], args, relpath):
            elem.decompose()
        else:
            del elem["hpp-unless"]

def apply_attribute_conditionals(soup, args, relpath):
    prefix = "hpp-"
    for elem in [tag for tag in iter_tags(soup) if any(k.startswith(prefix) for k in tag.attrs.keys())]:
        attr_keys = list(elem.attrs.keys())
        conditional_attrs = []
        for key in attr_keys:
            if not key.startswith(prefix) or not key.endswith("-if") or key == "hpp-if":
                continue
            attr_name = key[len(prefix):-len("-if")]
            if not attr_name:
                warn(f"Malformed conditional attribute '{key}' in {relpath}")
                del elem[key]
                continue
            conditional_attrs.append((key, attr_name))

        for if_key, attr_name in conditional_attrs:
            then_key = f"{prefix}{attr_name}-then"
            else_key = f"{prefix}{attr_name}-else"
            condition = eval_hpp_expr(elem[if_key], args, relpath)
            value = None
            should_set = False
            if condition:
                should_set = True
                if then_key in elem.attrs:
                    value = format_hpp_value(elem[then_key], args)
            elif else_key in elem.attrs:
                should_set = True
                value = format_hpp_value(elem[else_key], args)

            if should_set:
                if attr_name == "class" and value is not None:
                    add_class(elem, value)
                elif value is None:
                    elem[attr_name] = ""
                else:
                    elem[attr_name] = value

            del elem[if_key]
            if then_key in elem.attrs:
                del elem[then_key]
            if else_key in elem.attrs:
                del elem[else_key]

        for key in [k for k in list(elem.attrs.keys()) if k.startswith(prefix) and (k.endswith("-then") or k.endswith("-else"))]:
            warn(f"Conditional attribute helper '{key}' has no matching -if in {relpath}")
            del elem[key]

def collect_slots(hpp, relpath):
    slots = {}
    for slot in hpp.find_all("hpp-slot", recursive=False):
        name = slot.get("name")
        if name is None:
            warn(f"Slot without a name in {relpath}, ignoring...")
            continue
        if name in slots:
            warn(f"Duplicate slot named '{name}' in {relpath}, using the last one")
        slots[name] = [copy.copy(content) for content in slot.contents]
    return slots

def has_hpp_ancestor(elem):
    parent = elem.parent
    while isinstance(parent, Tag):
        if parent.name == "hpp":
            return True
        parent = parent.parent
    return False

def apply_slots(soup, slots):
    for slot in [tag for tag in list(iter_tags(soup)) if tag.name == "hpp-slot" and not has_hpp_ancestor(tag)]:
        name = slot.get("name")
        if name in slots:
            for content in slots[name]:
                slot.insert_before(copy.copy(content))
        slot.decompose()

def inflate_hpp(soup, deps, templates, relpath, inherited_args=None):
    for hpp in list(iter_hpp_tags(soup)):
        if hpp.parent is None or hpp.attrs is None:
            continue
        template_name = hpp.get("template")
        if template_name is None:
            warn(f"No template specified in {relpath}, ignoring...")
            continue

        try:
            flatten_deps(template_name, deps)
        except CircularDepException as exc:
            warn(f"{exc} in {relpath} > {deps.get(relpath)}, skipping...")

        # Append the dep so we reload in case this template is created
        deps.setdefault(relpath, set()).add(template_name)
        if template_name not in templates:
            warn(f"No template named '{template_name}' in {relpath}, ignoring...")
            continue
        args = build_hpp_args(hpp, inherited_args)
        slots = collect_slots(hpp, relpath)

        for ts in templates[template_name]:
            template_soup = copy.copy(ts)
            apply_slots(template_soup, slots)
            apply_element_conditionals(template_soup, args, template_name)
            apply_attribute_conditionals(template_soup, args, template_name)
            for dynamic_text_elem in template_soup.find_all("hpp-text"):
                name = dynamic_text_elem.get("name")
                if name in args:
                    dynamic_text_elem.string = (dynamic_text_elem.string or "") + args[name]
                    dynamic_text_elem.unwrap()
                else:
                    dynamic_text_elem.decompose()

            for dynamic_text_elem in [tag for tag in iter_tags(template_soup) if "hpp-text" in tag.attrs]:
                format_str = dynamic_text_elem.get("hpp-text")
                del dynamic_text_elem["hpp-text"]
                new_text = format_hpp_text(format_str, args)
                if new_text is not None:
                    dynamic_text_elem.string = new_text

            prefix = "hpp-"
            for dynamic_elem in [tag for tag in iter_tags(template_soup) if any(k.startswith(prefix) for k in tag.attrs.keys())]:
                for key in [k for k in dynamic_elem.attrs.keys() if k.startswith(prefix)]:
                    format_str = dynamic_elem[key]
                    del dynamic_elem[key]
                    attr_name = key[len(prefix):]
                    args.didLookup = False
                    new_value = format_str.format_map(args)
                    if not args.didLookup:
                        new_value = args.get(format_str, None)
                    if new_value is not None:
                        dynamic_elem[key[len(prefix):]] = new_value

            template_fragment = BeautifulSoup("", "html.parser")
            template_fragment.append(template_soup)
            hpp.insert_before(inflate_hpp(template_fragment, deps, templates, template_name, args))

        hpp.decompose()
    return soup

def rewrite_root_relative_urls(soup, url_prefix):
    if not url_prefix:
        return

    url_prefix = url_prefix.strip("/")
    if not url_prefix:
        return
    url_prefix = "/" + url_prefix

    def rewrite_url(url):
        if not url or not url.startswith("/") or url.startswith("//"):
            return url
        if url == url_prefix or url.startswith(url_prefix + "/"):
            return url
        return url_prefix + url

    def rewrite_srcset(srcset):
        rewritten_sources = []
        for source in srcset.split(","):
            parts = source.strip().split(None, 1)
            if not parts:
                continue
            parts[0] = rewrite_url(parts[0])
            rewritten_sources.append(" ".join(parts))
        return ", ".join(rewritten_sources)

    for tag in soup.find_all(True):
        for attr in ("href", "src"):
            if attr in tag.attrs:
                tag.attrs[attr] = rewrite_url(tag.attrs[attr])
        if "srcset" in tag.attrs:
            tag.attrs["srcset"] = rewrite_srcset(tag.attrs["srcset"])

def genhtml(inpath, outpath, templates, inroot, deps_map, url_prefix=""):
    relpath = os.path.relpath(inpath, inroot)
    print(f"= Processing {relpath}")

    deps = {}
    with open(inpath) as fp:
        soup = inflate_hpp(BeautifulSoup(fp, "html.parser"), deps, templates, relpath)
    rewrite_root_relative_urls(soup, url_prefix)
    deps_map[relpath] = flatten_deps(relpath, deps)

    ensure_parent(outpath)
    with open(outpath, "w") as fp:
        fp.write(str(soup))
    print(f"    * Wrote {outpath}")

def load_template(template_path):
    with open(template_path) as fp:
        print(f"+ Loading {os.path.splitext(os.path.basename(template_path))[0]}: ", end='')
        bs = BeautifulSoup(fp, "html.parser")
        res = [content for content in bs.contents if isinstance(content, Tag) or isinstance(content, BeautifulSoup)]
        if len(res) > 0:
            print("Success")
            return res
        else:
            print("Failed, no tags :(")
            return None

def load_templates(template_dirs):
    templates = {}
    template_sources = {}
    for templates_dir in template_dirs:
        if not os.path.isdir(templates_dir):
            warn(f"{templates_dir} is not a directory, skipping")
            continue

        for template in os.listdir(templates_dir):
            template_path = os.path.join(templates_dir, template)
            if not os.path.isfile(template_path):
                continue

            template_name = os.path.splitext(template)[0]
            loaded = load_template(template_path)
            if loaded is not None:
                templates[template_name] = loaded
                template_sources[template_name] = template_path

    return templates, template_sources

def template_source_root(template_path, template_dirs):
    for template_dir in template_dirs:
        if is_subdir(template_dir, template_path):
            return template_dir
    return None

def source_root(path, in_dirs):
    for in_dir in sorted(in_dirs, key=len, reverse=True):
        if is_subdir(in_dir, path):
            return in_dir
    return None

def overlay_file(relpath, in_dirs):
    for in_dir in reversed(in_dirs):
        path = os.path.join(in_dir, relpath)
        if os.path.exists(path):
            return path, in_dir
    return None, None

def iter_overlay_files(in_dirs, template_dirs):
    overlay = {}
    template_dirs_set = set(template_dirs)
    for in_dir in in_dirs:
        for root, dirs, files in os.walk(in_dir):
            dirs[:] = [
                d for d in dirs
                if os.path.abspath(os.path.join(root, d)).rstrip("/") not in template_dirs_set
            ]
            for file in files:
                filepath = os.path.join(root, file)
                rel_filepath = os.path.relpath(filepath, in_dir)
                overlay[rel_filepath] = (filepath, in_dir)
    return overlay

def copyfile(src, dst, **kwargs):
    ensure_parent(dst)
    print(f"* Copying {src} -> {dst}")
    shutil.copyfile(src, dst, **kwargs)

def process_file(filepath, dest_filepath, templates, in_dir, deps_map, url_prefix=""):
    if not os.path.exists(filepath):
        return
    if not filepath.endswith("html"):
        copyfile(filepath, dest_filepath)
    else:
        genhtml(filepath, dest_filepath, templates, in_dir, deps_map, url_prefix)

def process_relpath(relpath, in_dirs, out_dir, templates, deps_map, url_prefix=""):
    filepath, in_dir = overlay_file(relpath, in_dirs)
    if filepath is None:
        return False
    process_file(filepath, os.path.join(out_dir, relpath), templates, in_dir, deps_map, url_prefix)
    return True

def is_subdir(base, s):
    return os.path.commonpath([base, s]) == base

def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--in-dir",
        action="append",
        default=None,
        help="Specify directories containing input files. Can be passed multiple times; later values override earlier ones."
    )
    parser.add_argument(
        "--templates",
        action="append",
        default=None,
        help="Specify directories containing templates. Can be passed multiple times; later values override earlier ones."
    )
    parser.add_argument("--out-dir", default="sitegen", help="Specify the output directory. All files from --in-dir will be copied/processed into this directory")
    parser.add_argument("--listen", default=False, action="store_true", help="If specified, stay alive & process files as they change")
    parser.add_argument("--clean", default=False, action="store_true", help="If specified, cleans the out directory before starting")
    parser.add_argument("--port", default=8000, type=int, help="Specify the port to listen to when --listen is specified")
    parser.add_argument("--autoreload", default=False, action="store_true", help="Auto reload/navigate to the webpage that was last edited")
    parser.add_argument("--url-prefix", default="", help="Prefix root-relative HTML href/src/srcset URLs, e.g. /project for GitHub project Pages")

    parsed_args = parser.parse_args()
    parsed_in_dirs = parsed_args.in_dir if parsed_args.in_dir is not None else ["site"]
    in_dirs = [os.path.abspath(in_dir).rstrip("/") for in_dir in parsed_in_dirs]
    in_dir = in_dirs[-1]
    out_dir = os.path.abspath(parsed_args.out_dir)
    if parsed_args.templates is None:
        templates_dirs = [os.path.join(in_dir, "hpp") for in_dir in in_dirs]
    else:
        templates_dirs = []
        for templates_arg in parsed_args.templates:
            if "{in_dir}" in templates_arg:
                templates_dirs.extend(
                    os.path.abspath(templates_arg.format(in_dir=in_dir)).rstrip("/")
                    for in_dir in in_dirs
                )
            else:
                templates_dirs.append(os.path.abspath(templates_arg).rstrip("/"))

    if parsed_args.clean:
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)

    templates, _ = load_templates(templates_dirs)
    templates = {k: v for k, v in templates.items() if v is not None}

    deps_map = DepsMap()

    for rel_filepath, (filepath, in_dir) in iter_overlay_files(in_dirs, templates_dirs).items():
        dest_filepath = os.path.join(out_dir, rel_filepath)
        process_file(filepath, dest_filepath, templates, in_dir, deps_map, parsed_args.url_prefix)

    if parsed_args.listen:
        from watchdog import events as wd_events
        from watchdog.events import FileSystemMovedEvent, DirDeletedEvent, FileDeletedEvent
        from watchdog.observers.fsevents import FSEventsObserver as Observer

        autoreloader = AutoReloader(parsed_args.autoreload, parsed_args.port, deps_map)

        class LiveSiteEventHandler(wd_events.FileSystemEventHandler):
            def __init__(self, in_dirs, out_dir, templates, templates_dirs, deps_map, url_prefix):
                self.in_dirs = in_dirs
                self.out_dir = out_dir
                self.templates = templates
                self.templates_dirs = templates_dirs
                self.deps_map = deps_map
                self.url_prefix = url_prefix

            def refresh_templates(self):
                self.templates, _ = load_templates(self.templates_dirs)
                self.templates = {k: v for k, v in self.templates.items() if v is not None}
                return self.templates

            def dispatch(self, event):
                if event.is_directory:
                    return

                processed_template = False
                if isinstance(event, FileSystemMovedEvent):
                    if template_source_root(event.dest_path, self.templates_dirs) is not None and event.dest_path.endswith(".html"):
                            self.process_template(event.dest_path)
                            processed_template = True
                    if template_source_root(event.src_path, self.templates_dirs) is not None and event.src_path.endswith(".html"):
                            self.drop_template(event.src_path)
                            processed_template = True
                elif isinstance(event, DirDeletedEvent) or isinstance(event, FileDeletedEvent):
                    if template_source_root(event.src_path, self.templates_dirs) is not None and event.src_path.endswith(".html"):
                        self.drop_template(event.src_path)
                        processed_template = True
                elif template_source_root(event.src_path, self.templates_dirs) is not None and event.src_path.endswith(".html"):
                        self.process_template(event.src_path)
                        processed_template = True

                if processed_template:
                    return

                try:
                    super().dispatch(event)
                except Exception:
                    traceback.print_exc()

            def drop_template(self, template_path):
                template = os.path.splitext(os.path.basename(template_path))[0]
                self.refresh_templates()
                print(f"= Updating Deps for {template}")
                for dep in self.deps_map.removeTemplate(template):
                    process_relpath(dep, self.in_dirs, self.out_dir, self.templates, self.deps_map, self.url_prefix)

            def process_template(self, template_path):
                template = os.path.splitext(os.path.basename(template_path))[0]
                self.refresh_templates()
                print(f"= Updating Deps for {template}")
                deps = self.deps_map.getDepsOfTemplate(template)
                for dep in deps:
                    process_relpath(dep, self.in_dirs, self.out_dir, self.templates, self.deps_map, self.url_prefix)
                autoreloader.reloadIfNecessary(deps)

            def on_created(self, event):
                #print(f"[Created] {event.src_path}")
                self.on_created_path(event.src_path)

            def on_deleted(self, event):
                #print(f"[Deleted] {event.src_path}")
                self.on_deleted_path(event.src_path)

            def on_modified(self, event):
                #print(f"[Modified] {event.src_path}")
                self.on_modified_path(event.src_path)

            def on_moved(self, event):
                #print(f"[Moved] {event.src_path} -> {event.dest_path}")
                self.on_deleted_path(event.src_path)
                self.on_created_path(event.dest_path)

            def on_created_path(self, path):
                self.on_modified_path(path)

            def on_deleted_path(self, path):
                in_dir = source_root(path, self.in_dirs)
                if in_dir is None:
                    return
                rel = os.path.relpath(path, in_dir)
                if process_relpath(rel, self.in_dirs, self.out_dir, self.templates, self.deps_map, self.url_prefix):
                    autoreloader(rel)
                    return
                out_path = os.path.join(self.out_dir, rel)
                if not os.path.exists(out_path):
                    return
                print(f"* Removing {out_path}")
                if os.path.isdir(out_path):
                    os.rmdir(out_path)
                else:
                    os.remove(out_path)

            def on_modified_path(self, path):
                if os.path.isdir(path):
                    return
                if template_source_root(path, self.templates_dirs) is not None:
                    return
                in_dir = source_root(path, self.in_dirs)
                if in_dir is None:
                    return
                rel = os.path.relpath(path, in_dir)
                process_relpath(rel, self.in_dirs, self.out_dir, self.templates, self.deps_map, self.url_prefix)
                autoreloader(rel)

        event_handler = LiveSiteEventHandler(in_dirs, out_dir, templates, templates_dirs, deps_map, parsed_args.url_prefix)

        observer = Observer()
        for in_dir in in_dirs:
            observer.schedule(event_handler, in_dir, recursive=True)
        for templates_dir in templates_dirs:
            if not any(is_subdir(in_dir, templates_dir) for in_dir in in_dirs):
                observer.schedule(event_handler, templates_dir, recursive=True)
        observer.start()
        try:
            os.chdir(out_dir)
            Thread(target=autoreloader.openAfterDelay, args=[0.1]).run()
            test(SimpleHTTPRequestHandler, port=parsed_args.port)
        finally:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main(sys.argv[1:])
