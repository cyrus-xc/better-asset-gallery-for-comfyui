// Adapter for ComfyUI frontend 1.58.5. Asset menus have no public extension hook.
const patched = Symbol("better-assets-handler");

export function* components(vnode, seen = new Set()) {
  if (!vnode || seen.has(vnode)) return;
  seen.add(vnode);
  if (vnode.component) {
    yield vnode.component;
    yield* components(vnode.component.subTree, seen);
  }
  if (vnode.suspense) yield* components(vnode.suspense.activeBranch, seen);
  if (Array.isArray(vnode.children)) {
    for (const child of vnode.children) yield* components(child, seen);
  }
}

export function descriptor(asset) {
  const url = new URL(asset.preview_url || asset.url || "/", location.href);
  const type = asset.tags?.find(tag => tag === "input" || tag === "output") || url.searchParams.get("type") || asset.type || "output";
  if (asset.loader_path) {
    const path = asset.loader_path.replaceAll("\\", "/");
    const slash = path.lastIndexOf("/");
    return {filename: path.slice(slash + 1), subfolder: slash < 0 ? "" : path.slice(0, slash), type};
  }
  if (asset.user_metadata?.jobId) {
    return {filename: asset.name, subfolder: asset.user_metadata.subfolder, type};
  }
  return {
    filename: asset.filename || url.searchParams.get("filename") || asset.name,
    subfolder: url.searchParams.get("subfolder") || asset.user_metadata?.subfolder || asset.subfolder || "",
    type,
  };
}

export function fileKey(file) {
  return JSON.stringify([file.type, file.subfolder === "." ? "" : file.subfolder, file.filename]);
}

export function assetIdentity(asset) {
  return asset?.user_metadata?.jobId || JSON.stringify([asset?.id, asset?.created_at, asset?.updated_at]);
}

export function installNativeAssets({app, deleteAssets, showMetadata, isDeleted, labels}) {
  const wrapped = new WeakSet();
  let tab, frame, contextAssets = [];
  const name = component => component.type.__name || component.type.name;
  const stores = app.extensionManager._p._s;

  function availableAssets() {
    if (!tab) return [];
    const assets = [];
    for (const component of components(tab.subTree)) {
      if (name(component) === "AssetsSidebarGridView") assets.push(...component.vnode.props.assets);
      if (name(component) === "AssetsSidebarListView") assets.push(...component.vnode.props["selectable-assets"]);
    }
    return [...new Map(assets.map(asset => [asset.id, asset])).values()];
  }

  function selection(asset) {
    const ids = stores.get("assetSelection").selectedAssetIds;
    const selected = availableAssets().filter(item => ids.has(item.id));
    return selected.length > 1 && ids.has(asset.id) ? selected : [asset];
  }

  function replaceHandler(component, event, factory) {
    const props = component.vnode.props;
    const original = props?.[event];
    if (!original || original[patched]) return;
    const handler = factory(original);
    handler[patched] = true;
    props[event] = handler;
  }

  function beforeRender(component) {
    const kind = name(component);
    if (kind === "AssetsSidebarGridView" || kind === "AssetsSidebarListView") {
      replaceHandler(component, "onContextMenu", original => (event, asset) => {
        contextAssets = selection(asset);
        original(event, asset);
        schedule();
      });
      if (kind === "AssetsSidebarGridView") {
        setArray(component, "assets", component.vnode.props.assets.filter(asset => !isDeleted(descriptor(asset), asset)));
      } else {
        setArray(component, "assetItems", component.vnode.props["asset-items"].filter(item => !isDeleted(descriptor(item.asset), item.asset)));
        setArray(component, "selectableAssets", component.vnode.props["selectable-assets"].filter(asset => !isDeleted(descriptor(asset), asset)));
      }
    }
    if (kind === "MediaAssetSelectionBar") {
      replaceHandler(component, "onDelete", () => () => {
        const ids = stores.get("assetSelection").selectedAssetIds;
        void deleteAssets(availableAssets().filter(asset => ids.has(asset.id)));
      });
    }
    if (kind === "ContextMenu" && contextAssets.length) {
      const model = component.props.model;
      let item = model.find(entry => entry.icon?.includes("trash"));
      if (!item) {
        item = {key: "better-assets-delete", icon: "icon-[lucide--trash-2]"};
        model.push(item);
      }
      const nextLabel = contextAssets.length > 1 ? labels().deleteSelected : labels().delete;
      if (item.key !== "better-assets-delete" || item.label !== nextLabel) {
        Object.assign(item, {key: "better-assets-delete", label: nextLabel, visible: true, command: () => void deleteAssets([...contextAssets])});
      }
    }
    if (kind === "MediaLightbox") {
      const original = component.vnode.props["all-gallery-items"];
      const assets = availableAssets();
      const kept = original.filter(item => {
        const asset = assets.find(asset => asset.preview_url === item.url);
        return !isDeleted(descriptor(asset || item), asset);
      });
      setArray(component, "allGalleryItems", kept);
      const active = original[component.vnode.props["active-index"]];
      component.props.activeIndex = kept.indexOf(active);
      const event = "onUpdate:activeIndex";
      const previous = component.vnode.props[event];
      const originalHandler = previous?.[patched] || previous;
      if (originalHandler) {
        const handler = index => originalHandler(index < 0 ? -1 : original.indexOf(kept[index]));
        handler[patched] = originalHandler;
        component.vnode.props[event] = handler;
      }
    }
  }

  function setArray(component, property, values) {
    const current = component.props[property];
    if (current.length !== values.length || values.some((value, index) => current[index] !== value)) component.props[property] = values;
  }

  function wrap(component) {
    if (wrapped.has(component)) { beforeRender(component); return; }
    wrapped.add(component);
    const render = component.render;
    component.render = function(...args) {
      beforeRender(component);
      return render.apply(this, args);
    };
    component.update();
  }

  function scan() {
    frame = null;
    if (!tab || tab.isUnmounted) {
      tab = [...components(document.getElementById("vue-app")?._vnode)].find(component => name(component) === "AssetsSidebarTab");
    }
    if (!tab) { showMetadata(null); return; }
    let activePreview = null;
    for (const component of components(tab.subTree)) {
      if (["AssetsSidebarGridView", "AssetsSidebarListView", "MediaAssetSelectionBar", "ContextMenu", "MediaLightbox"].includes(name(component))) wrap(component);
      if (name(component) === "MediaLightbox" && component.props.activeIndex >= 0) {
        const item = component.props.allGalleryItems[component.props.activeIndex];
        const asset = availableAssets().find(asset => asset.preview_url === item?.url);
        const root = [...document.querySelectorAll('[role="dialog"][data-mask]')].find(element => element.querySelector("img,video"));
        if (root && item) activePreview = {root, file: descriptor(asset || item)};
      }
    }
    showMetadata(activePreview);
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(scan);
  }

  const observer = new MutationObserver(schedule);
  observer.observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["src"]});
  schedule();

  return {
    async refresh() {
      await stores.get("queue").updateFresh();
      await Promise.all([stores.get("assets").outputAssets.invalidate(), stores.get("assets").inputAssets.invalidate()]);
      stores.get("assetSelection").clearSelection();
      if (tab && !tab.isUnmounted) {
        for (const component of components(tab.subTree)) {
          if (wrapped.has(component)) component.update();
        }
      }
      schedule();
    },
  };
}
