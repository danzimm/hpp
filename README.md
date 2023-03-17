# dzwebgen

I didn't like how complex all the website preprocessing systems were, so I made my own simple one. The main goal here is to make a JS-free website which reuses HTML across pages (e.g. the navigation).

`webgen` fulfills this requirement by introducing a meta-element `<webgen />` which allows you to specify a template which will be pasted in. Each template can take various parameters in order to customize how the HTML is pasted (this allows you to customize element attributes or insert text directly into the pasted HTML).

Oh, also this is in plain python with minimal deps (BeautifulSoup and optionally watchdog if you want to leverage the `--listen` option).

Speaking of the `--listen` option: the tool also allows you to spin up a webserver which will automatically process file changes as you make them so you don't need to manually re-run the preprocessor.
