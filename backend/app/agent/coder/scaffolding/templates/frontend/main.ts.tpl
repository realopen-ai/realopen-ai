import "./style.css";
import { mountCrud, createCrudContext } from "./crud";
import { config } from "./generated";
import { installPlugins } from "./extensions";
// FEATURE_IMPORTS

const context = createCrudContext(config);
document.title = config.name;
const closePlugins = installPlugins(context, [/* FEATURE_PLUGINS */]);
const root = document.querySelector<HTMLElement>("#app")!;
const mountApp = (root: HTMLElement) => mountCrud(root, config, context);
const closeApp = /* FEATURE_MOUNT */;
if (import.meta.hot)
  import.meta.hot.dispose(() => {
    closeApp();
    closePlugins();
  });
