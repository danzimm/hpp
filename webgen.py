#!/usr/bin/env python3

import argparse
import copy
import os
import shutil
import subprocess
import sys
import time
import traceback

from bs4 import BeautifulSoup
from bs4.element import Tag
from http.server import test, SimpleHTTPRequestHandler

def ensure_parent(p):
    parent = os.path.dirname(p)
    if not os.path.exists(parent):
        os.mkdir(parent)

def is_webgen_attr(tag):
    return any(k.startswith("wg-") for k in tag.attrs.keys())

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

    def didNavigate(self, navigated):
        pass

    @property
    def baseUrl(self):
        return f"http://127.0.0.1:{self.port}"

    def viewingWebgen(self, current_url=None):
        if current_url is None:
            current_url = self.getCurrentUrl()
        return current_url.startswith(self.baseUrl)

    def __call__(self, path):
        if path.startswith("webgen/"):
            current_url = self.getCurrentUrl()
            if not self.viewingWebgen(current_url):
                return
            current_url_path = current_url[len(self.baseUrl):]
            if current_url_path.startswith("/"):
                current_url_path = current_url_path[1:]
            if current_url_path.endswith("/"):
                current_url_path = current_url_path + "index.html"
            template_path = path[len("webgen/"):]
            template = os.path.splitext(template_path)[0]
            deps = self.deps_map.getDepsOfTemplate(template)
            if len(deps) == 1:
                self(next(iter(deps)))
            elif current_url_path in deps:
                self.reload()
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

    def open(self, url_path):
        self.runAppleScript(f"""tell application "Safari"
set docURL to "http://127.0.0.1:{self.port}{url_path}"
set URL of document 1 to docURL
end tell
""")

    def reload(self):
        if not self.viewingWebgen():
            return
        self.runAppleScript("""tell application "Safari"
set docUrl to URL of document 1
set URL of document 1 to docURL
end tell""")

    def getCurrentUrl(self):
        return self.runAppleScript("""on run
  tell application "Safari"
    return URL of document 1
  end tell
end run""", return_stdout=True).strip()

    def runAppleScript(self, script, return_stdout=False):
        try:
            cp = subprocess.run(["osascript", "-e", script], check=True, capture_output=return_stdout, text=True if return_stdout else None)
            if return_stdout:
                return cp.stdout
        except subprocess.CalledProcessError as exc:
            warn(f"Failed to run {script}: {exc}")

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

def inflate_webgen(soup, deps, templates, relpath):
    for webgen in soup.find_all("webgen"):
        template_name = webgen.get("template")
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
        args = SafeFormatDict(webgen.attrs)

        for ts in templates[template_name]:
            template_soup = copy.copy(ts)
            for wg_text in template_soup.find_all("wg-text"):
                name = wg_text.get("name")
                if name in args:
                    wg_text.string = (wg_text.string or "") + args[name]
                    wg_text.unwrap()
                else:
                    wg_text.decompose()

            for dynamic_elem in template_soup.find_all(is_webgen_attr):
                for key in [k for k in dynamic_elem.attrs.keys() if k.startswith("wg-")]:
                    format_str = dynamic_elem[key]
                    del dynamic_elem[key]
                    attr_name = key[3:]
                    args.didLookup = False
                    new_value = format_str.format_map(args)
                    if not args.didLookup:
                        new_value = args[format_str] if format_str else None
                    if new_value is not None:
                        dynamic_elem[key[3:]] = new_value

            webgen.insert_before(inflate_webgen(template_soup, deps, templates, template_name))

        webgen.decompose()
    return soup

def genhtml(inpath, outpath, templates, inroot, deps_map):
    relpath = os.path.relpath(inpath, inroot)
    print(f"= Processing {relpath}")

    deps = {}
    with open(inpath) as fp:
        soup = inflate_webgen(BeautifulSoup(fp, "html.parser"), deps, templates, relpath)
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

def copyfile(src, dst, **kwargs):
    ensure_parent(dst)
    print(f"* Copying {src} -> {dst}")
    shutil.copyfile(src, dst, **kwargs)

def process_file(filepath, dest_filepath, templates, in_dir, deps_map):
    if not os.path.exists(filepath):
        return
    if not filepath.endswith("html"):
        copyfile(filepath, dest_filepath)
    else:
        genhtml(filepath, dest_filepath, templates, in_dir, deps_map)

def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-dir", default="site", help="Specify the directory containing the input files to be copied/processed into --out-dir")
    parser.add_argument("--out-dir", default="sitegen", help="Specify the output directory. All files from --in-dir will be copied/processed into this directory")
    parser.add_argument("--listen", default=False, action="store_true", help="If specified, stay alive & process files as they change")
    parser.add_argument("--clean", default=False, action="store_true", help="If specified, cleans the out directory before starting")
    parser.add_argument("--port", default=8000, type=int, help="Specify the port to listen to when --listen is specified")
    parser.add_argument("--autoreload", default=False, action="store_true", help="Auto reload/navigate to the webpage that was last edited")

    parsed_args = parser.parse_args()
    in_dir = os.path.abspath(parsed_args.in_dir)
    out_dir = os.path.abspath(parsed_args.out_dir)

    if parsed_args.clean:
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)

    webgen_dir = os.path.join(in_dir, "webgen")
    templates = {
        os.path.splitext(template)[0]: load_template(os.path.join(webgen_dir, template))
        for template in os.listdir(webgen_dir)
    }
    templates = {k: v for k, v in templates.items() if v is not None}

    deps_map = DepsMap()

    for root, dirs, files in os.walk(in_dir):
        if root == in_dir:
            if "webgen" in dirs:
                dirs.remove("webgen")
        for file in files:
            filepath = os.path.join(root, file)
            rel_filepath = os.path.relpath(filepath, in_dir)
            dest_filepath = os.path.join(out_dir, rel_filepath)
            process_file(filepath, dest_filepath, templates, in_dir, deps_map)

    if parsed_args.listen:
        from watchdog import events as wd_events
        from watchdog.observers.fsevents import FSEventsObserver as Observer

        autoreloader = AutoReloader(parsed_args.autoreload, parsed_args.port, deps_map)

        class WebGenEventHandler(wd_events.FileSystemEventHandler):
            def __init__(self, in_dir, out_dir, templates, deps_map):
                self.in_dir = in_dir
                self.webgen_dir = os.path.join(in_dir, "webgen")
                self.out_dir = out_dir
                self.templates = templates
                self.deps_map = deps_map

            def dispatch(self, event):
                if event.is_directory:
                    return

                processed_template = False
                if isinstance(event, wd_events.FileSystemMovedEvent):
                    if os.path.commonpath([self.webgen_dir, event.dest_path]) == self.webgen_dir:
                        if event.dest_path.endswith(".html"):
                            self.process_template(event.dest_path)
                            processed_template = True
                    if os.path.commonpath([self.webgen_dir, event.src_path]) == self.webgen_dir:
                        if event.src_path.endswith(".html"):
                            self.drop_template(event.src_path)
                elif os.path.commonpath([self.webgen_dir, event.src_path]) == self.webgen_dir:
                    if event.src_path.endswith(".html"):
                        self.process_template(event.src_path)
                        processed_template = True

                if processed_template:
                    return

                try:
                    super().dispatch(event)
                except Exception:
                    traceback.print_exc()

            def drop_template(self, webgen_path):
                template_path = os.path.relpath(webgen_path, self.webgen_dir)
                template = os.path.splitext(template_path)[0]
                print(f"= Updating Deps for {template}")
                for dep in deps_map.removeTemplate(template):
                    process_file(os.path.join(self.in_dir, dep), os.path.join(self.out_dir, dep), self.templates, self.in_dir, self.deps_map)

            def process_template(self, webgen_path):
                template_path = os.path.relpath(webgen_path, self.webgen_dir)
                template = os.path.splitext(template_path)[0]
                new_temp = load_template(webgen_path)
                if new_temp is not None:
                    self.templates[template] = new_temp
                print(f"= Updating Deps for {template}")
                for dep in deps_map.getDepsOfTemplate(template):
                    process_file(os.path.join(self.in_dir, dep), os.path.join(self.out_dir, dep), self.templates, self.in_dir, self.deps_map)

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
                rel = os.path.relpath(path, self.in_dir)
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
                rel = os.path.relpath(path, self.in_dir)
                process_file(path, os.path.join(self.out_dir, rel), self.templates, self.in_dir, self.deps_map)
                autoreloader(rel)

        event_handler = WebGenEventHandler(in_dir, out_dir, templates, deps_map)

        observer = Observer()
        observer.schedule(event_handler, in_dir, recursive=True)
        observer.start()
        try:
            os.chdir(out_dir)
            test(SimpleHTTPRequestHandler, port=parsed_args.port)
        finally:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main(sys.argv[1:])
