from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/open_sprite_pipeline/web"


class ShellParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.scripts = []
        self.tabs = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script" and "src" in attrs:
            self.scripts.append(attrs["src"])
        if "data-main-tab" in attrs:
            self.tabs.append(attrs["data-main-tab"])
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise AssertionError(f"Unbalanced HTML tag: {tag}")


class ForgeUiTests(unittest.TestCase):
    def test_generate_board_blockout_review_and_cutout_sources(self):
        source = (WEB / "forge.js").read_text()
        for token in ("Generate blockout from photos", "generateToggle", "generateHeightHint", "generateFloorHeight",
                      "generateLatestCutouts", "generateCutouts", "/v1/ui/runs?limit=5", 'run.status === "completed"',
                      'body.append("segment_refs"', 'body.append("height_hint"', 'body.append("floor_height"',
                      "class BlockoutReview", "blockoutReview-${job.id}", "blockout-renders", "blockout-params",
                      "blockout-palette", "blockout-assumptions", "blockout-next-view", "/blockout/regenerate",
                      "job.generate.regenerations >= 8", 'key: "cutout_index"', 'fieldset.disabled = !!job.match.submitted'):
            self.assertIn(token, source)
        self.assertLess(source.index('<div class="forge-blockout-review"'), source.index('<div class="forge-review"'))
        self.assertIn('aria-label="Bake Forge: sheets, iterations, and blockouts from photos"', (WEB / "index.html").read_text())

    def test_shell_tabs_modules_and_balanced_html(self):
        shell = ShellParser()
        shell.feed((WEB / "index.html").read_text())
        self.assertEqual(shell.stack, [])
        self.assertEqual(len(shell.ids), len(set(shell.ids)))
        self.assertEqual(shell.tabs, ["capture", "forge", "library"])
        for name in ("app", "viewer", "forge"):
            self.assertIn(f"/assets/{name}.js", shell.scripts)
        for name in ("selectionCanvas", "imageInput", "meshViewer", "resultSection"):
            self.assertIn(name, shell.ids)

    def test_viewer_extraction_and_four_forge_views(self):
        app = (WEB / "app.js").read_text()
        viewer = (WEB / "viewer.js").read_text()
        forge = (WEB / "forge.js").read_text()
        self.assertNotIn("class MeshViewer", app)
        self.assertIn('import { MeshViewer, loadViewerModules } from "./viewer.js"', app)
        self.assertIn("export class MeshViewer", viewer)
        for name in ("BoardView", "MatchReview", "LibraryView", "VersionDetail"):
            self.assertIn(f"class {name}", forge)
        # P3 class and module-loader bodies are frozen by digest, including whitespace.
        klass = viewer[viewer.index("export class MeshViewer"):].replace("export class", "class", 1)
        loader = viewer[viewer.index("export async function"):viewer.index("\nexport class")].replace("export async", "async", 1)
        self.assertEqual(hashlib.sha256(klass.encode()).hexdigest(), "6196db4c4c2da0e29a7e2a53fc91f15b4b54d074c3952fdb108536dd08aa38d7")
        self.assertEqual(hashlib.sha256(loader.encode()).hexdigest(), "90219fb6814f1760a961990aac1277d1defd3384e8a76a0819a451cfd5f5c09b")

    def test_capture_logic_is_byte_identical_after_extraction(self):
        app = (WEB / "app.js").read_text()
        # Gen Ladder GL1 explicitly adds import controls; keep the original
        # checksum for every byte outside those additions.
        start = app.index("// Gen Ladder GL1: imports use server-side files")
        end = app.index("// --- refresh persistence:", start)
        app = app[:start] + app[end:]
        app = app.replace("  libraryRunKey: null,\n", "")
        app = app.replace("  configureLibrarySave(null);\n", "")
        app = app.replace("  configureLibrarySave(result);\n", "")
        body = app.removeprefix('import { MeshViewer, loadViewerModules } from "./viewer.js";\n')
        body = body.removesuffix('\nexport { api, toast, escapeHtml };\n')
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), "c465501b29c02f07b72f8f36fb2ab0195dbceb01d6bc61a03e78764d021d0fea")

    def test_gen_ladder_gl1_controls_and_single_navigation_seam(self):
        shell = ShellParser()
        shell.feed((WEB / "index.html").read_text())
        for name in ("librarySaveForm", "libraryAsset", "libraryVariant", "saveToLibrary", "openSavedLibrary"):
            self.assertIn(name, shell.ids)
        app = (WEB / "app.js").read_text()
        forge = (WEB / "forge.js").read_text()
        self.assertIn('/v1/ui/runs/${encodeURIComponent(recordKey)}/library', app)
        self.assertEqual(app.count('new Event("forge:open-library")'), 1)
        self.assertEqual(forge.count('document.addEventListener("forge:open-library"'), 1)
        self.assertNotIn('from "./forge.js"', app)
        self.assertIn('version.origin === "trellis" ? "" : button("Iterate"', forge)
        self.assertIn('version.job_id == null ? {} : api(', forge)
        self.assertIn('if (v.job_id == null)', forge)
        self.assertIn('pretty(v.inputs.run || {})', forge)

    def test_javascript_module_syntax(self):
        self.assertIsNotNone(shutil.which("node"), "Node is required for the static JS syntax gate.")
        with tempfile.TemporaryDirectory(prefix="forge-p4-js-") as tmp:
            for name in ("app", "viewer", "forge"):
                with self.subTest(module=name):
                    output = Path(tmp) / f"{name}.log"
                    with output.open("w") as log:
                        result = subprocess.run(["node", "--input-type=module", "--check"],
                                                input=(WEB / f"{name}.js").read_text(), text=True,
                                                stdout=log, stderr=subprocess.STDOUT, timeout=15)
                    self.assertEqual(result.returncode, 0, output.read_text())

    def test_layer_visibility_uses_part_id_map_for_nested_mesh_nodes(self):
        source = (WEB / "forge.js").read_text()
        method = source[source.index("  applyLayers() {"):source.index("\n  tab(name)")]
        probe = '''
const nodes = [
  {name: 'wall', isMesh: true},
  {name: 'primitive', isMesh: true, parent: {name: 'banner'}},
  {name: 'unknown', isMesh: true},
];
const detail = {
  root: {querySelectorAll: () => [{dataset: {layer: 'core'}, checked: true}, {dataset: {layer: 'social'}, checked: false}]},
  version: {part_layer_map: {wall: 'core', banner: 'social'}},
  viewer: {object: {traverse: (fn) => nodes.forEach(fn)}},
METHOD
};
detail.applyLayers();
if (nodes[0].visible !== true || nodes[1].visible !== false || nodes[2].visible !== undefined) throw Error('Incorrect layer mapping');
console.log('Layer visibility probe passed');
'''.replace("METHOD", method)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.log"
            with path.open("w") as log:
                result = subprocess.run(["node", "--input-type=module"], input=probe, text=True,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=15)
            self.assertEqual(result.returncode, 0, path.read_text())


if __name__ == "__main__":
    unittest.main()
