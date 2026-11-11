import assert from "node:assert/strict";
import { test } from "node:test";
import { descriptor } from "../web/native-assets.js";

globalThis.location = new URL("http://localhost:8188/");

test("a job's video filename wins over its PNG preview", () => {
  const target = descriptor({
    name: "movie.mp4", tags: ["output"],
    preview_url: "/api/view?filename=movie_preview.png&subfolder=previews&type=output",
    user_metadata: {jobId: "example-job", subfolder: "album"},
  });
  assert.deepEqual(target, {filename: "movie.mp4", subfolder: "album", type: "output"});
});

test("a renamed input keeps its stored path and root", () => {
  const target = descriptor({
    name: "Friendly name.png", tags: ["input"], loader_path: "references/original.jpg",
    preview_url: "/api/view?filename=preview.png&type=output&subfolder=previews",
  });
  assert.deepEqual(target, {filename: "original.jpg", subfolder: "references", type: "input"});
});

test("the native lightbox's hardcoded output type does not relabel an input URL", () => {
  const target = descriptor({
    filename: "reference.png", subfolder: "references", type: "output",
    url: "/api/view?filename=reference.png&subfolder=references&type=input",
  });
  assert.deepEqual(target, {filename: "reference.png", subfolder: "references", type: "input"});
});
