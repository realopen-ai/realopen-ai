:root { font-size: 100%; }
body { padding: 2rem 0; }
main { max-width: 1000px; }
nav { display: flex; gap: .75rem; flex-wrap: wrap; align-items: center; margin: 1rem 0; }
.toolbar { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
.toolbar > input { flex: 2; min-width: 10rem; }
.toolbar > select { flex: 1; min-width: 8rem; }
.toolbar > button { width: auto; }
form[aria-label] { padding: 1.25rem; border: 1px solid var(--app-border); border-radius: .75rem; }
section[aria-label="Records"] { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr)); gap: 1rem; margin-top: 1.5rem; }
article { margin: 0; }
article button { margin-right: .5rem; }
dt { color: var(--app-muted); font-size: .875rem; }
dd { margin: .25rem 0 .75rem; overflow-wrap: anywhere; white-space: pre-wrap; }
[role="status"] { min-height: 1.5em; }
.error { color: var(--app-danger); }
button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid var(--app-primary); outline-offset: 3px; }
