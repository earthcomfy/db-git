# Documentation development and publishing

The site uses MkDocs with the Material theme. Edit Markdown files under `docs/`
and update `nav` in the root `mkdocs.yml` when adding or moving pages.

## Preview locally

From the repository root, run:

```bash
uv tool run --with-requirements requirements-docs.txt mkdocs serve
```

Open the local URL printed by MkDocs. The preview reloads when pages change.
Documentation tools run separately from the application's dependencies.

## Validate the site

```bash
uv tool run --with-requirements requirements-docs.txt mkdocs build --strict
```

The build writes static HTML to `site/`, which is ignored by Git. Strict validation
fails on missing pages, broken relative links, and invalid internal anchors.
Review both desktop and mobile navigation when changing the theme or layout.

Keep the README short and link to the detailed guides. Document behavior changes
alongside code, especially engine-specific limits and recovery guarantees.
The local roadmap is excluded from the generated site and search index.

## Publish with GitHub Pages

The `docs` GitHub Actions workflow validates documentation on pull requests and
pushes to `main`. Only pushes to `main` or manual runs on `main` publish the site;
pull requests never deploy it.

For the first deployment, a repository administrator must select **Settings →
Pages → Build and deployment → Source → GitHub Actions**. Then push the site
changes to `main`, or run the `docs` workflow manually once it exists there.
If the `github-pages` environment requires approval, approve its deployment.

The configured URL is <https://earthcomfy.github.io/db-git/>. Publication requires
the repository's Pages settings and the workflow to succeed; a local build does
not publish anything.

The workflow uses GitHub's Pages artifact deployment and does not maintain a
`gh-pages` branch. If the repository owner/name or domain changes, update
`site_url` in `mkdocs.yml`, the README docs link, and the package documentation URL.

## Documentation versions

The site publishes from `main` independently of package releases, so documentation
corrections can go live without a new PyPI release. There is one documentation
site without a version selector. When a feature needs a version distinction, add
an "Added in version X" note beside that feature using its actual release version.
