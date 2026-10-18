import "./style.css";
import { mountCrud, createCrudContext } from "./crud";
import { config } from "./generated";
import { installPlugins } from "./extensions";
// FEATURE_IMPORTS

const context = createCrudContext(config);
document.title = config.name;
const closePlugins = installPlugins(context, [/* FEATURE_PLUGINS */]);
const closeApp = mountCrud(
  document.querySelector<HTMLElement>("#app")!,
  config,
  context,
);
if (import.meta.hot)
  import.meta.hot.dispose(() => {
    closeApp();
    closePlugins();
  });
