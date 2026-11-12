import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { assetIdentity, descriptor, fileKey, installNativeAssets } from "./native-assets.js";
import { createMetadataPanel } from "./metadata-panel.js";

const translations = {
  zh: {delete: "删除", deleteSelected: "删除所选资产", confirm: "永久删除所选资产及对应的硬盘文件？相关历史记录和资产引用也会同步清理。", deleted: "删除完成", failed: "删除失败", partial: "部分文件未能删除", refreshFailed: "文件已删除，请刷新页面更新资产库。", title: "资产信息", loading: "正在读取图片信息…", noMetadata: "图片中未保存提示词或生成参数。", copy: "复制", copied: "已复制", copyFailed: "浏览器未允许复制，请选中文本后按 Ctrl+C。", file: "文件", prompts: "提示词", models: "模型", parameters: "生成参数", positive: "正向提示词", negative: "负向提示词", notFilled: "未填写", instruction: "编辑指令", text: "文本", embedded: "来自图片元数据", history: "来自生成历史", path: "路径", size: "大小", dimensions: "尺寸", format: "格式", modified: "修改时间", noFile: "无法确定这个资产的文件路径"},
  en: {delete: "Delete", deleteSelected: "Delete selected assets", confirm: "Permanently delete the selected assets and their files from disk? Related history and asset references will also be cleaned up.", deleted: "Deleted", failed: "Deletion failed", partial: "Some files could not be deleted", refreshFailed: "Files deleted. Refresh the page to update the gallery.", title: "Asset details", loading: "Reading image metadata…", noMetadata: "No prompts or generation parameters were saved in this image.", copy: "Copy", copied: "Copied", copyFailed: "Clipboard access was denied. Select the text and copy it with your keyboard.", file: "File", prompts: "Prompts", models: "Models", parameters: "Generation parameters", positive: "Positive prompt", negative: "Negative prompt", notFilled: "Not provided", instruction: "Edit instruction", text: "Text", embedded: "From image metadata", history: "From generation history", path: "Path", size: "Size", dimensions: "Dimensions", format: "Format", modified: "Modified", noFile: "Cannot resolve this asset's file path"},
};

function labels() {
  const locale = app.extensionManager.setting.get("Comfy.Locale") || navigator.language;
  return translations[String(locale).startsWith("zh") ? "zh" : "en"];
}

const deleted = new Set();
let native;

async function deleteAssets(assets) {
  if (!assets.length) return;
  const text = labels();
  const files = assets.map(asset => asset.user_metadata?.outputCount > 1
    ? {job_id: asset.user_metadata.jobId}
    : descriptor(asset));
  if (files.some(file => !file.job_id && !file.filename)) {
    app.extensionManager.toast.error(text.noFile);
    return;
  }
  const confirmed = await app.extensionManager.dialog.confirm({
    key: "better-assets-delete", type: "delete", title: "ComfyUI", message: text.confirm,
    itemList: assets.map(asset => {
      const file = descriptor(asset);
      return `${file.type}/${file.subfolder ? file.subfolder + "/" : ""}${file.filename}${asset.user_metadata?.outputCount > 1 ? ` × ${asset.user_metadata.outputCount}` : ""}`;
    }),
  });
  if (!confirmed) return;
  try {
    const response = await api.fetchApi("/better-assets/delete", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({files}),
    });
    const result = await response.json();
    if (!response.ok || result.sync_error) throw new Error(result.sync_error || result.error || response.statusText);
    for (const file of result.files || []) {
      for (const asset of assets) {
        if (asset.user_metadata?.outputCount > 1 || fileKey(descriptor(asset)) === fileKey(file)) deleted.add(JSON.stringify([fileKey(file), assetIdentity(asset)]));
      }
    }
    if (result.files?.length) {
      try { await native.refresh(); }
      catch { app.extensionManager.toast.warning(text.refreshFailed); }
    }
    if (result.errors.length) {
      app.extensionManager.toast.warning(text.partial, {description: result.errors.map(error => `${error.filename}: ${error.error}`).join("\n")});
    } else {
      app.extensionManager.toast.success(`${text.deleted} · ${result.deleted}`);
    }
  } catch (error) {
    app.extensionManager.toast.error(text.failed, {description: error.message});
  }
}

app.registerExtension({
  name: "better-asset-gallery-for-comfyui",
  setup() {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = new URL("./gallery.css", import.meta.url).href;
    document.head.append(css);
    native = installNativeAssets({app, labels, deleteAssets, isDeleted: (file, asset) => !!asset && deleted.has(JSON.stringify([fileKey(file), assetIdentity(asset)])), showMetadata: createMetadataPanel({api, app, labels})});
  },
});
