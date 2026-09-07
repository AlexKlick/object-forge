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
    def test_authored_spec_style_form_review_and_version_seams(self):
        source = (WEB / "forge.js").read_text()
        for token in ('specToggle', 'specAsset', 'specVariant', 'specVariants', 'specPaletteOnly',
                      'styleFields', 'styleEnabled', 'styleSeeds', 'styleStrength', 'styleIpScale',
                      'styleControlScale', 'stylePrompt', 'style_refs[]', '"from_spec"', '${forge}/catalog',
                      'No catalog published yet — start the host worker', 'Drop style references (≤ 6)',
                      'body.append("spec_asset"', 'body.append("palette_only"', 'body.append("style"',
                      'class StyleReview', 'choose(view, seed)', 'data-action="use-seed"',
                      'Style candidates', 'panel.decision || "reject"', 'STYLE-TRUNCATED',
                      'Authored spec — edit the blockout from the version instead',
                      '"Style"', 'style/report.json', 'loading="lazy"',
                      '<span class="forge-badge">styled</span>'):
            self.assertIn(token, source)
        blocks = ['<div class="forge-blockout-review"', '<div class="forge-style-review"', '<div class="forge-review"']
        self.assertEqual(sorted(source.index(block) for block in blocks), [source.index(block) for block in blocks])
        for token in ('.forge-style-review', '.forge-style-row', '.is-chosen', '.forge-style-chips'):
            self.assertIn(token, (WEB / 'app.css').read_text())

    @unittest.skipUnless(shutil.which('node'), 'node is required for JavaScript probes')
    def test_style_seed_choice_preserves_other_views_and_predecisions(self):
        source = (WEB / 'forge.js').read_text()
        methods = source[source.index('  decision(panel) {'):source.index('  missing() {')]
        probe = r'''
const $ = () => ({checked: true});
const panels = [
  {panel_id: 'sfront-1-p0', source: 'style', style_view: 'front', seed: 1, decision: 'accept', view: 'front'},
  {panel_id: 'sfront-2-p0', source: 'style', style_view: 'front', seed: 2, decision: 'reject', view: 'front'},
  {panel_id: 'sback-1-p0', source: 'style', style_view: 'back', seed: 1, decision: 'accept', view: 'back'},
  {panel_id: 'u0-p0', source: 'upload', decision: 'reject', view: 'front'},
];
const calls = [];
const review = {job: {match: {panels}}, decisions: new Map(),
  drawSelected() { calls.push('selected'); }, drawSheets() { calls.push('sheets'); },
  coverage() { calls.push('coverage'); }, save() { calls.push('save'); },
METHODS
};
if (panels.map(p => review.decision(p).decision).join() !== 'accept,reject,accept,reject') throw Error('Lost predecisions');
review.choose('front', 2);
if (panels.map(p => review.decision(p).decision).join() !== 'reject,accept,accept,reject') throw Error('Nonexclusive seed choice');
if (review.decision(panels[1]).view !== 'front' || review.selected !== panels[1]) throw Error('Wrong selected view');
if (calls.join() !== 'selected,sheets,coverage,save') throw Error('Draft refresh/save missing');
review.submitting = true; review.choose('front', 1);
if (review.decision(panels[1]).decision !== 'accept') throw Error('Changed a submitting review');
console.log('Seed decision probe passed');
'''.replace('METHODS', methods.replace('\n  }', '\n  },'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'seed-probe.log'
            with path.open('w') as log:
                result = subprocess.run(['node', '--input-type=module'], input=probe, text=True,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=15)
            self.assertEqual(result.returncode, 0, path.read_text())

    @unittest.skipUnless(shutil.which('node'), 'node is required for JavaScript probes')
    def test_style_multipart_modes_and_palette_only_guard(self):
        source = (WEB / 'forge.js').read_text()
        method = source[source.index('  async create() {'):source.index('  card(job) {')]
        probe = r'''
let fields, sent;
const $ = (selector) => fields[selector];
const forge = '/v1/forge';
const toast = () => {};
const api = async (path, options) => { sent = options.body; return {}; };
const board = {root: {querySelectorAll: () => []}, files: ['photo'], segmentRefs: new Map(),
  replacements: new Map(), styleRefs: ['reference'], card() {},
METHOD
};
async function run(intent, {palette = false, enabled = true, prompt = ''} = {}) {
  sent = null;
  fields = {'button[type=submit]': {}, '#forgeFormError': {},
    '#iterateToggle': {checked: false}, '#generateToggle': {checked: intent === 'generate'},
    '#specToggle': {checked: intent === 'from_spec'}, '#specAsset': {value: 'authored'},
    '[name=asset]': {value: 'alias'}, '[name=variant]': {value: 'photo_variant'}, '#specVariant': {value: 'undeclared'},
    '#specPaletteOnly': {checked: palette}, '#styleFields': {hidden: palette || intent === 'fresh'},
    '#styleEnabled': {checked: enabled}, '#styleSeeds': {value: '3'}, '#styleStrength': {value: '0.62'},
    '#styleIpScale': {value: '0.6'}, '#styleControlScale': {value: '0.8'}, '#stylePrompt': {value: prompt},
    '#generateHeightHint': {value: '12'}, '#generateFloorHeight': {value: '3'}};
  await board.create();
  if (!sent) throw Error(fields['#forgeFormError'].textContent || 'No request');
}
await run('from_spec');
if (sent.get('intent') !== 'from_spec' || sent.get('asset') !== 'alias' || sent.get('spec_asset') !== 'authored' || sent.get('variant') !== 'undeclared') throw Error('Spec identity/alias lost');
if (sent.has('files') || sent.get('palette_only') !== 'false' || sent.getAll('style_refs[]').length !== 1) throw Error('Wrong spec uploads');
let style = JSON.parse(sent.get('style'));
if (style.seeds_per_view !== 3 || style.strength !== 0.62 || style.ip_scale !== 0.6 || style.control_scale !== 0.8 || 'prompt_override' in style) throw Error('Wrong style settings');
await run('from_spec', {palette: true});
if (sent.has('style') || sent.has('style_refs[]') || sent.get('palette_only') !== 'true') throw Error('Palette-only leaked style');
await run('from_spec', {enabled: false});
if (sent.has('style') || sent.has('style_refs[]')) throw Error('Disabled style leaked');
await run('generate', {prompt: '  Painted workshop  '});
if (JSON.parse(sent.get('style')).prompt_override !== 'Painted workshop' || sent.get('files') !== 'photo' || sent.has('spec_asset')) throw Error('Wrong generate payload');
await run('fresh');
if (sent.has('style') || sent.has('style_refs[]') || sent.get('files') !== 'photo') throw Error('Fresh job leaked style');
console.log('Multipart mode probe passed');
'''.replace('METHOD', method)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'multipart-probe.log'
            with path.open('w') as log:
                result = subprocess.run(['node', '--input-type=module'], input=probe, text=True,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=15)
            self.assertEqual(result.returncode, 0, path.read_text())

    def test_edit_blockout_gating_form_and_inherited_review_sources(self):
        source = (WEB / "forge.js").read_text()
        self.assertIn('version.artifacts.includes("blockout/spec.yaml") ? button("Edit blockout", "edit-blockout"', source)
        for token in ('editBlockout()', 'name="height"', 'name="floor_height"', 'name="plinth_floors"',
                      'name="tower_enabled"', 'name="tower_width"', 'name="tower_location"',
                      'value="rear_center"', 'value="front_center"', 'value="center"',
                      'input.pattern = "[0-9a-fA-F]{6}"', 'input.oninput = preview',
                      'body.append("intent", "iterate_blockout")', 'body.append("parent_job", version.job_id)',
                      'body.append("parent_version", version.number)', 'body.append("edit", JSON.stringify(edit))',
                      'form.remove(); showTab("forge"); board.card(job)', 'key: "parent_upload_index"',
                      'upload.job_id || this.job.id', '["generate", "iterate_blockout"].includes(job.intent)'):
            self.assertIn(token, source)

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
