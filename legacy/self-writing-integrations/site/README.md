# Public site

A static site with no build step: five sheets (`index.html`, `how-it-works.html`, `console.html`,
`docs.html`, `changelog.html`) plus `404.html`, sharing `styles.css`, `site.js` and `favicon.svg`.
Console screenshots live in `assets/`.

Serve it from this folder with any static server, for example:

```
cd site
python -m http.server 8080      # http://127.0.0.1:8080
```

The "Open the console" links point at `/integrations`, so put the site and the developer console
behind the same host in deployment, or change those links to the console's URL.
