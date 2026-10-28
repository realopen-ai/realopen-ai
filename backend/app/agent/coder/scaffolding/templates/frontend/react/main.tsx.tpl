import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { App } from "./App";
import { config } from "./generated";
import { createCrudContext } from "./data";
import { installPlugins } from "./extensions";
// FEATURE_IMPORTS

document.title = config.name;
const context = createCrudContext(config);
const closePlugins = installPlugins(context, [/* FEATURE_PLUGINS */]);
const root = createRoot(document.getElementById("root")!);
root.render(
  <StrictMode>
    {/* FEATURE_ROOT */}
  </StrictMode>,
);
if (import.meta.hot)
  import.meta.hot.dispose(() => {
    root.unmount();
    closePlugins();
  });
