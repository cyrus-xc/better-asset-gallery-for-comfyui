import { fileKey } from "./native-assets.js";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

export function createMetadataPanel({api, app, labels}) {
  let root, panel, key, controller;

  function section(title) {
    const section = element("section", "bag-section");
    section.append(element("h3", "bag-section-title", title));
    panel.append(section);
    return section;
  }

  function copyButton(value) {
    const button = element("button", "bag-copy", labels().copy);
    button.type = "button";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(value);
        app.extensionManager.toast.success(labels().copied);
      } catch {
        app.extensionManager.toast.warning(labels().copyFailed);
      }
    });
    return button;
  }

  function render(data) {
    panel.replaceChildren(element("h2", "bag-heading", labels().title));
    const file = data.file;
    const details = section(labels().file);
    details.append(element("p", "bag-filename", file.filename));
    const info = element("dl", "bag-info");
    const values = [
      [labels().path, `${file.type}/${file.subfolder ? file.subfolder + "/" : ""}${file.filename}`],
      [labels().size, fileSize(file.size)],
      [labels().dimensions, file.width ? `${file.width} × ${file.height}` : "—"],
      [labels().format, [file.format, file.mode].filter(Boolean).join(" · ") || "—"],
      [labels().modified, new Date(file.modified * 1000).toLocaleString()],
    ];
    for (const [label, value] of values) info.append(element("dt", "", label), element("dd", "", value));
    details.append(info);
    if (data.source) panel.append(element("p", "bag-source", labels()[data.source]));
    for (const prompt of data.prompts) {
      const block = element("details", "bag-section bag-prompt-section");
      const heading = element("summary", "bag-section-title", labels()[prompt.role] || labels().prompts);
      const content = element("div", "bag-prompt-content");
      const empty = !prompt.text;
      if (!empty) content.append(copyButton(prompt.text));
      content.append(element("p", "bag-node", `${prompt.node_type} #${prompt.node_id} · ${prompt.field}`), element("p", empty ? "bag-prompt bag-empty" : "bag-prompt", empty ? labels().notFilled : prompt.text));
      block.append(heading, content);
      panel.append(block);
    }
    if (data.models.length) {
      const block = section(labels().models);
      for (const model of data.models) {
        block.append(element("p", "bag-node", `${model.field} · #${model.node_id}`), element("p", "bag-value", model.name));
      }
    }
    if (data.parameters.length) {
      const block = section(labels().parameters);
      const table = element("dl", "bag-info");
      for (const parameter of data.parameters) table.append(element("dt", "", `${parameter.field} · #${parameter.node_id}`), element("dd", "", String(parameter.value)));
      block.append(table);
    }
    if (data.parameters_text) {
      const block = section(labels().parameters);
      block.append(copyButton(data.parameters_text), element("p", "bag-prompt", data.parameters_text));
    }
    if (!data.prompts.length && !data.parameters.length && !data.models.length && !data.parameters_text) {
      panel.append(element("p", "bag-empty", labels().noMetadata));
    }
    for (const [title, raw] of [["API prompt", data.prompt_json], ["Workflow", data.workflow_json]]) {
      if (!raw) continue;
      const disclosure = element("details", "bag-raw");
      disclosure.append(element("summary", "", title), copyButton(raw), element("pre", "", raw));
      panel.append(disclosure);
    }
  }

  return preview => {
    if (!preview) {
      controller?.abort();
      root?.classList.remove("bag-lightbox");
      panel?.remove();
      root = panel = key = null;
      return;
    }
    const nextKey = fileKey(preview.file);
    if (root === preview.root && key === nextKey) return;
    controller?.abort();
    if (root !== preview.root) {
      root?.classList.remove("bag-lightbox");
      panel?.remove();
      root = preview.root;
      root.classList.add("bag-lightbox");
      panel = element("aside", "bag-panel");
      panel.setAttribute("aria-label", labels().title);
      panel.addEventListener("keydown", event => { if (event.key !== "Escape") event.stopPropagation(); });
      root.append(panel);
    }
    key = nextKey;
    panel.replaceChildren(element("h2", "bag-heading", labels().title), element("p", "bag-empty", labels().loading));
    controller = new AbortController();
    const signal = controller.signal;
    api.fetchApi(`/better-assets/metadata?${new URLSearchParams(preview.file)}`, {signal})
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.error || response.statusText); return data; })
      .then(data => { if (!signal.aborted) render(data); })
      .catch(error => { if (!signal.aborted) panel.replaceChildren(element("h2", "bag-heading", labels().title), element("p", "bag-empty bag-error", error.message)); });
  };
}
