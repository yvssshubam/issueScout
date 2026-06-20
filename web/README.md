# web/

Single-file frontend for IssueScout: `index.html` contains all markup, styles,
and JavaScript (no build step, no bundler). It is served by `../server.py`,
which also provides `POST /api/match`.

Onboarding flow: welcome -> stage fork -> contribution type -> profile
(languages / interests / task size / target companies) -> preview -> results.
The results screen fetches ranked issues from the backend and renders the
two-pane repos-left / stats-right layout.
