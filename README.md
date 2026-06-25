# hpp

I didn't like how complex all the website preprocessing systems were, so I made my own simple one. The main goal here is to make a JS-free website which reuses HTML across pages (e.g. the navigation). `hpp` stands for `html preprocessor`.

`hpp` fulfills this requirement by introducing a meta-element `<hpp />` which allows you to specify a template which will be pasted in. Each template can take various parameters in order to customize how the HTML is pasted (this allows you to customize element attributes or insert text directly into the pasted HTML).

Oh, also this is in plain python with minimal deps (BeautifulSoup and optionally watchdog if you want to leverage the `--listen` option).

Speaking of the `--listen` option: the tool also allows you to spin up a webserver which will automatically process file changes as you make them so you don't need to manually re-run the preprocessor.

For project-hosted sites, such as GitHub Pages repositories served below `/<repo>/`, pass `--url-prefix /<repo>` to prefix root-relative `href`, `src`, and `srcset` URLs in generated HTML.

`--in-dir` and `--templates` can both be passed multiple times. Later directories override earlier ones, which lets a site use a shared theme as a base and then layer local pages, assets, and templates on top:

```sh
python3 hpp/hpp.py \
  --in-dir theme/site \
  --in-dir site
```

By default, this also loads templates from each input directory's `hpp/` directory in the same order. Pass `--templates` explicitly when you want a different template overlay.

## Conditionals

Templates can keep or remove elements with simple conditionals:

```html
<p hpp-if="description">...</p>
<p hpp-unless="compact">...</p>
```

Templates can also conditionally set attributes:

```html
<li hpp-class-if="key == current" hpp-class-then="selected">
<a hpp-aria-current-if="key == current" hpp-aria-current-then="page">
<img hpp-loading-if="eager" hpp-loading-then="eager" hpp-loading-else="lazy" />
```

The supported expression forms are `name`, `!name`, `name == name`, `name != name`, `name == "literal"`, and `name != "literal"`. Missing values, empty strings, `false`, `0`, and `no` are treated as false.

## Slots

Templates can expose named slots for caller-provided markup:

```html
<!-- hpp/panel.html -->
<section>
  <h1 hpp-text="{title}"></h1>
  <hpp-slot name="body" />
</section>
```

Callers fill slots with matching `<hpp-slot>` children:

```html
<hpp template="panel" title="Hello">
  <hpp-slot name="body">
    <p>Panel body</p>
  </hpp-slot>
</hpp>
```

Missing slots are removed. Slotted content is expanded with the template call's arguments, and nested `<hpp>` calls inherit parent arguments unless they override them explicitly.
