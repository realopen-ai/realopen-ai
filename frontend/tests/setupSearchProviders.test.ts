import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

const { useSetupStore } = await import("../src/store/setupStore.ts");

test("setup preserves chosen search providers across remounts and submits them", async () => {
  useSetupStore.setState({ searchProviders: [], searchProvidersLoaded: false });
  let loads = 0;
  let saved: Record<string, unknown> | undefined;
  const mock = installFetch((url, init) => {
    if (url === "/api/setup/search-providers") {
      loads++;
      return jsonResponse([
        {
          id: "searxng",
          name: "SearXNG",
          enabled: true,
          recommended: true,
          local: true,
        },
        {
          id: "degoog",
          name: "Degoog",
          enabled: false,
          recommended: false,
          local: true,
        },
      ]);
    }
    assert.equal(url, "/api/setup/apply");
    saved = JSON.parse(String(init?.body));
    return jsonResponse({ status: "applied" });
  });
  try {
    await useSetupStore.getState().loadSearchProviders();
    useSetupStore.getState().toggleSearchProvider("searxng");
    useSetupStore.getState().toggleSearchProvider("degoog");
    await useSetupStore.getState().loadSearchProviders();
    assert.equal(loads, 1);
    assert.equal(await useSetupStore.getState().applySetup(), true);
    assert.deepEqual(saved?.search_providers, { searxng: false, degoog: true });
  } finally {
    mock.restore();
  }
});
