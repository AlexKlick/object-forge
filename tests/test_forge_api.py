from __future__ import annotations

import base64
from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from open_sprite_pipeline.api import create_app
from open_sprite_pipeline.interactive_segmentation import ColorFloodSegmenter


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


class ForgeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = Path(self.tmp.name) / "ui"
        env = patch.dict(os.environ, {"FORGE_ENABLED": "1", "FORGE_WORKER_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)
        self.app = self.make_app(self.ui)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.store = self.app.state.forge_store

    def make_app(self, ui):
        return create_app(config_path=ROOT / "configs/app.example.yaml", ui_root=ui,
                          segmenter=ColorFloodSegmenter(), allow_real_generation=False)

    def catalog_payload(self):
        return {"specs": [{"asset": "a", "variants": ["default", "v"], "views": ["front"]}],
                "prompts": ["a"], "assets_root": "/host/assets", "published_at": "2026-09-07T12:00:00+00:00",
                "errors": [{"file": "specs/broken.yaml", "error": "Invalid YAML"}]}

    def test_catalog_auth_roundtrip_and_persistence(self):
        empty = {"specs": [], "prompts": [], "published_at": None}
        self.assertEqual(self.client.get('/v1/forge/catalog').json(), empty)
        payload = self.catalog_payload()
        with patch.dict(os.environ, {'FORGE_WORKER_TOKEN': 'catalog-test'}):
            self.assertEqual(self.client.post('/v1/forge/worker/catalog', json=payload).status_code, 403)
            response = self.client.post('/v1/forge/worker/catalog', json=payload,
                                        headers={'X-Forge-Worker': 'catalog-test'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), payload)
            self.assertEqual(self.client.get('/v1/forge/catalog').json(), payload)
        from open_sprite_pipeline.forge_store import ForgeStore
        self.assertEqual(ForgeStore(self.store.root).get_catalog(), payload)
        self.assertEqual(json.loads((self.store.root / 'catalog.json').read_text()), payload)
        self.assertFalse((self.store.root / 'catalog.json.tmp').exists())

    def test_catalog_rejects_bad_shapes_without_replacing_document(self):
        payload = self.catalog_payload()
        self.store.save_catalog(payload)
        invalid = [[], {}, {**payload, 'extra': True}, {**payload, 'specs': [None]},
                   {**payload, 'specs': payload['specs'] * 501}, {**payload, 'prompts': ['a'] * 501},
                   {**payload, 'prompts': 'a'}, {**payload, 'prompts': ['../a']},
                   {**payload, 'assets_root': 'a' * 201}, {**payload, 'published_at': None},
                   {**payload, 'published_at': 'yesterday'}, {**payload, 'errors': {}},
                   {**payload, 'errors': [{'file': 'bad', 'error': 'x' * 201}]},
                   {**payload, 'errors': [{'file': 12, 'error': 'bad'}]}]
        for key, value in [('asset', '../escape'), ('asset', 4), ('asset', 'x' * 201),
                           ('variants', {}), ('variants', [3]), ('variants', ['../bad']),
                           ('views', 'front'), ('views', ['../bad'])]:
            invalid.append({**payload, 'specs': [{**payload['specs'][0], key: value}]})
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/v1/forge/worker/catalog', json=body).status_code, 422)
                self.assertEqual(self.store.get_catalog(), payload)

    def test_from_spec_catalog_validation_allows_alias_and_empty_catalog(self):
        fields = {'asset': 'alias', 'variant': 'undeclared', 'intent': 'from_spec',
                  'spec_asset': 'other', 'palette_only': 'true'}
        self.assertEqual(self.client.post('/v1/forge/jobs', data=fields).status_code, 201)
        payload = self.catalog_payload()
        payload['specs'].append({'asset': 'b', 'variants': [], 'views': ['front']})
        self.store.save_catalog(payload)
        before = len(self.store.list_jobs())
        response = self.client.post('/v1/forge/jobs', data=fields)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['detail'], 'Unknown spec asset other; known: a, b')
        self.assertEqual(len(self.store.list_jobs()), before)
        response = self.client.post('/v1/forge/jobs', data={**fields, 'spec_asset': 'a'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['asset'], 'alias')
        self.assertEqual(response.json()['variant'], 'undeclared')
        self.store.save_catalog({**payload, 'specs': []})
        self.assertEqual(self.client.post('/v1/forge/jobs', data=fields).status_code, 201)

    def test_from_spec_style_refs_routes_auth_lease_and_validation(self):
        fields = {'asset': 'a', 'variant': 'v', 'intent': 'from_spec',
                  'style': '{"enabled": true}', 'canonical_views': '["front"]'}
        refs = [('style_refs[]', ('ref.png', png_bytes(), 'image/png'))] * 6
        response = self.client.post('/v1/forge/jobs', data=fields, files=refs)
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        self.assertEqual(len(job['style_refs']), 6)
        base = f"/v1/forge/jobs/{job['id']}"
        response = self.client.get(base + '/style/refs/0')
        self.assertEqual(response.content, png_bytes())
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertEqual(self.client.get(base + '/style/refs/6').status_code, 404)
        self.assertEqual(self.client.post(base + '/blockout/regenerate', json={}).status_code, 409)
        job = self.store.claim_job(stages=['matching'], job_id=job['id'])
        lease = job['lease']['lease_id']
        route = base + '/style/front/1000.png'
        with patch.dict(os.environ, {'FORGE_WORKER_TOKEN': 'secret'}):
            self.assertEqual(self.client.post(route, content=png_bytes()).status_code, 403)
            headers = {'X-Forge-Worker': 'secret', 'X-Forge-Lease': 'stale'}
            self.assertEqual(self.client.post(route, content=png_bytes(), headers=headers).status_code, 409)
            headers['X-Forge-Lease'] = lease
            self.assertEqual(self.client.post(route, content=png_bytes(), headers=headers).status_code, 200)
            for path in ('/style/front/-1.png', '/style/unknown/1.png'):
                self.assertEqual(self.client.post(base + path, content=png_bytes(), headers=headers).status_code, 422)
            self.assertEqual(self.client.post(route, content=b'', headers=headers).status_code, 422)
            self.assertEqual(self.client.post(route, content=b'x' * (24 * 1024 * 1024 + 1), headers=headers).status_code, 422)
            report = {'views': {'front': {'seeds': [1000], 'chosen': 1000,
                       'metrics': {'1000': {'metrics': {'pass': True}, 'checks': {'pass': True}}}}},
                      'prompt_tokens': {'front': 30}, 'model': 'fake', 'refs': 6,
                      'params': job['generate']['style']}
            body = {'report': report, 'lease_id': lease}
            self.assertEqual(self.client.post(base + '/style', json=body).status_code, 403)
            self.assertEqual(self.client.post(base + '/style', json={**body, 'lease_id': 'stale'}, headers=headers).status_code, 409)
            self.assertEqual(self.client.post(base + '/style', json=body, headers=headers).status_code, 200)
            self.assertEqual(self.client.get(base + '/style').json(), report)
            self.assertEqual(self.client.get(route).content, png_bytes())
            invalid = {**report, 'views': {'front': {**report['views']['front'], 'chosen': 55}}}
            self.assertEqual(self.client.post(base + '/style', json={**body, 'report': invalid}, headers=headers).status_code, 422)
            self.store.worker_match(job['id'], {'panels': []}, lease)
            self.assertEqual(self.client.post(route, content=png_bytes(), headers=headers).status_code, 409)
            self.assertEqual(self.client.post(base + '/style', json=body, headers=headers).status_code, 409)
        before = len(self.store.list_jobs())
        for updates, files in (({}, refs + refs[:1]), ({'style': '{'}, []),
                               ({'style': '{"steps": 1}'}, []), ({'palette_only': 'true'}, []),
                               ({'spec_asset': '../x'}, []), ({'palette_only': 'perhaps'}, []),
                               ({}, [('style_refs', ('bad.png', b'bad', 'image/png'))])):
            with self.subTest(updates=updates):
                response = self.client.post('/v1/forge/jobs', data=fields | updates, files=files)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.store.list_jobs()), before)
        with patch.object(self.store, 'record_style_ref', side_effect=ValueError('write refused')):
            self.assertEqual(self.client.post('/v1/forge/jobs', data=fields, files=refs[:1]).status_code, 422)
        self.assertEqual(len(self.store.list_jobs()), before)

    def upload(self, **fields):
        response = self.client.post("/v1/forge/jobs", data={
            "asset": "chair", "variant": "oak", "params": "{}",
            "canonical_views": '["front", "back"]', **fields,
        }, files=[("files[]", ("sheet.png", png_bytes(), "image/png"))])
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def progress(self, job, state, **fields):
        response = self.client.post(f"/v1/forge/jobs/{job['id']}/progress", json={"state": state, **fields})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def reviewing(self):
        job = self.upload()
        job = self.progress(job, "matching")
        return self.progress(job, "review")

    def stage(self, job):
        response = self.client.post(f"/v1/forge/jobs/{job['id']}/review", json={
            "mode": "submit", "panels": [
                {"panel_id": "p1", "decision": "accept", "view": "front", "iou": 0.95},
                {"panel_id": "p2", "decision": "repin", "view": "back", "iou": 0.9},
            ]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def baking(self):
        job = self.stage(self.reviewing())
        response = self.client.post(f"/v1/forge/jobs/{job['id']}/approve")
        self.assertEqual(response.status_code, 200, response.text)
        return self.progress(response.json(), "baking")

    def completed(self):
        job = self.baking()
        response = self.client.post("/v1/forge/worker/complete", json={
            "job_id": job["id"], "artifacts": {"bake_report.json": {"iou": 0.97},
                                                "turntable/readme.txt": "frame notes"},
            "metrics": {"iou": 0.97}})
        self.assertEqual(response.status_code, 200, response.text)
        return job, response.json()

    def test_iterate_blockout_form_edit_and_parent_validation(self):
        parent = self.upload(intent="generate")
        parent["state"] = "baking"; self.store._save_job(parent)
        version = self.store.create_version(parent["id"], artifacts={"blockout/spec.yaml": b"asset: chair\n"})
        fields = {"asset": "chair", "variant": "oak", "intent": "iterate_blockout",
                  "parent_job": parent["id"], "parent_version": str(version["number"]), "edit": '{"height":18}'}
        response = self.client.post("/v1/forge/jobs", data=fields)
        self.assertEqual(response.status_code, 201, response.text)
        child = response.json()
        self.assertEqual(child["generate"]["edit"], {"height": 18})
        self.assertEqual(child["inputs"]["parent_uploads"], [0])
        self.assertEqual(self.client.get(f"/v1/forge/jobs/{child['id']}/uploads/0").content, png_bytes())
        for changes in ({"edit": "{}"}, {"edit": '{"palette":{"body":"#ffffff"}}'}, {"edit": '{"height":301}'},
                        {"edit": "{"}, {"parent_job": "missing"}, {"parent_version": "999"},
                        {"intent": "iterate_params"}, {"height_hint": "20"}, {"params": '{"edit":{"height":18}}'}):
            response = self.client.post("/v1/forge/jobs", data={**fields, **changes})
            self.assertTrue(400 <= response.status_code < 500, response.text)
        for omitted in ("parent_job", "parent_version", "edit"):
            response = self.client.post("/v1/forge/jobs", data={k: v for k, v in fields.items() if k != omitted})
            self.assertIn(response.status_code, (400, 422), response.text)
        other = self.upload(intent="generate")
        other["state"] = "baking"; self.store._save_job(other)
        missing = self.store.create_version(other["id"], artifacts={})
        response = self.client.post("/v1/forge/jobs", data={**fields, "parent_job": other["id"], "parent_version": str(missing["number"])})
        self.assertIn(response.status_code, (400, 422), response.text)
        fresh = self.upload()
        fresh["state"] = "baking"; self.store._save_job(fresh)
        non_generate = self.store.create_version(fresh["id"], artifacts={"blockout/spec.yaml": b"asset: chair\n"})
        response = self.client.post("/v1/forge/jobs", data={**fields, "parent_job": fresh["id"], "parent_version": str(non_generate["number"])})
        self.assertIn(response.status_code, (400, 422), response.text)

    def test_generate_form_validation_and_cutout_proxy(self):
        image = self.client.post("/v1/ui/uploads", files={"file": ("photo.png", png_bytes(), "image/png")}).json()
        response = self.client.post(f"/v1/ui/uploads/{image['image_id']}/segments", json={"points": [{"x": 4, "y": 4, "label": 1}]})
        self.assertEqual(response.status_code, 200, response.text)
        segment = response.json()
        ref = {"image_id": image["image_id"], "segment_id": segment["segment_id"]}
        fields = {"asset": "a", "variant": "default", "intent": "generate", "segment_refs": json.dumps([ref]), "height_hint": "18", "floor_height": "4"}
        result = self.client.post("/v1/forge/jobs", data=fields)
        self.assertEqual(result.status_code, 201, result.text)
        job = result.json(); base = f"/v1/forge/jobs/{job['id']}"
        self.assertEqual(job["generate"]["height_hint"], 18)
        self.assertEqual(job["generate"]["floor_height"], 4)
        proxy = self.client.get(base + "/cutouts/0.png")
        self.assertEqual(proxy.status_code, 200)
        self.assertEqual(proxy.content, self.app.state.store.segment_file(ref["image_id"], ref["segment_id"], "cutout").read_bytes())
        for index in (-1, 1):
            self.assertEqual(self.client.get(base + f"/cutouts/{index}.png").status_code, 404)
        badref = {**ref, "segment_id": "missing"}
        missing = self.client.post("/v1/forge/jobs", data={**fields, "segment_refs": json.dumps([badref])}).json()
        self.assertEqual(self.client.get(f"/v1/forge/jobs/{missing['id']}/cutouts/0.png").status_code, 404)
        for changed in ({"segment_refs": "[]"}, {"segment_refs": "bad"}, {"height_hint": "nan"}, {"floor_height": "11"}, {"segment_refs": json.dumps([ref] * 8)}):
            response = self.client.post("/v1/forge/jobs", data={**fields, **changed})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.post(base + "/blockout/regenerate", json={}).status_code, 409)

    def test_generate_worker_routes_and_regenerate_conflicts(self):
        from test_forge_generate_store import blockout_payload
        job = self.upload(intent="generate", canonical_views='["front"]', height_hint="21", floor_height="3")
        base = f"/v1/forge/jobs/{job['id']}"
        claim = self.store.claim_job(stages=["matching"], job_id=job["id"])
        lease = claim["lease"]["lease_id"]
        payload = blockout_payload(); spec = payload.pop("spec_yaml")
        body = {"blockout": payload, "lease_id": lease, "artifact": {"encoding": "base64", "data": base64.b64encode(spec).decode()}}
        self.assertEqual(self.client.post(base + "/renders/front.png", content=png_bytes()).status_code, 409)
        self.assertEqual(self.client.post(base + "/blockout", json={**body, "lease_id": "bad"}).status_code, 409)
        with patch.dict(os.environ, {"FORGE_WORKER_TOKEN": "private-test"}):
            self.assertEqual(self.client.post(base + "/blockout", json=body).status_code, 403)
            self.assertEqual(self.client.post(base + "/renders/front.png", content=png_bytes(), headers={"X-Forge-Lease": lease}).status_code, 403)
        self.assertEqual(self.client.post(base + "/renders/front.png", content=png_bytes(), headers={"X-Forge-Lease": lease}).status_code, 200)
        response = self.client.post(base + "/blockout", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(base + "/blockout").json(), payload)
        self.assertEqual(self.client.get(base + "/blockout/spec.yaml").content, spec)
        self.assertEqual(self.client.get(base + "/renders/front.png").content, png_bytes())
        self.store.worker_match(job["id"], {"panels": [], "views_missing": ["front"]}, lease)
        self.assertEqual(self.client.post(base + "/blockout", json=body).status_code, 409)
        response = self.client.post(base + "/blockout/regenerate", json={"height_hint": 30})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "matching")
        self.assertEqual(self.client.get(base + "/blockout").status_code, 404)
        self.assertEqual(self.client.post(base + "/blockout", json=body).status_code, 409)
        claim = self.store.claim_job(stages=["matching"], job_id=job["id"])
        self.store.worker_match(job["id"], {"panels": [], "views_missing": ["front"]}, claim["lease"]["lease_id"])
        self.store.review(job["id"], "submit", [], ["front"])
        self.assertEqual(self.client.post(base + "/blockout/regenerate", json={}).status_code, 409)
        fresh = self.reviewing()
        self.assertEqual(self.client.post(f"/v1/forge/jobs/{fresh['id']}/blockout/regenerate", json={}).status_code, 409)

    def test_disabled_router_absent_and_existing_responses_identical(self):
        snapshots = []
        for value in (None, "0", "false", "off"):
            with patch.dict(os.environ):
                if value is None:
                    os.environ.pop("FORGE_ENABLED", None)
                else:
                    os.environ["FORGE_ENABLED"] = value
                ui = Path(self.tmp.name) / f"disabled-{value}"
                app = self.make_app(ui)
                self.assertFalse(hasattr(app.state, "forge_store"))
                self.assertFalse((ui / "forge").exists())
                self.assertFalse(any(route.path.startswith("/v1/forge") for route in app.routes))
                with TestClient(app) as client:
                    self.assertEqual(client.get("/v1/forge/status").status_code, 404)
                    self.assertFalse(any(p.startswith("/v1/forge") for p in client.get("/openapi.json").json()["paths"]))
                    snapshots.append([client.get(path).content for path in ("/healthz", "/", "/openapi.json")])
        self.assertTrue(all(s == snapshots[0] for s in snapshots))

    def test_truthy_and_ui_root_precedence(self):
        with patch.dict(os.environ, {"FORGE_ENABLED": " YES ", "OPEN_SPRITE_UI_ROOT": str(Path(self.tmp.name) / "env-ui")}):
            explicit = self.make_app(self.ui)
            self.assertEqual(explicit.state.forge_store.root, self.ui / "forge")
            env_app = self.make_app(None)
            self.assertEqual(env_app.state.forge_store.root, Path(self.tmp.name) / "env-ui/forge")

    def test_empty_status_assets_jobs_and_library(self):
        status = self.client.get("/v1/forge/status").json()
        self.assertEqual(status, {"enabled": True, "store_root": str(self.ui / "forge"),
                                  "jobs_active": 0, "versions_total": 0})
        for path in ("jobs", "assets", "library"):
            self.assertEqual(self.client.get(f"/v1/forge/{path}").json(), [])
        self.assertEqual(self.client.post("/v1/forge/worker/claim", json={"kind": "pipeline"}).status_code, 204)
        self.assertEqual(self.client.post("/v1/forge/worker/claim", json={"kind": "critic"}).status_code, 204)

    def test_full_lifecycle_claim_review_complete_and_library(self):
        job = self.upload()
        self.assertEqual(job["state"], "uploaded")
        first = self.client.post("/v1/forge/worker/claim", json={"kind": "pipeline"}).json()
        self.assertEqual(first["state"], "matching")
        job = self.progress(job, "review", lease_id=first["lease"]["lease_id"], markers=["matched"])
        incomplete = {"mode": "submit", "panels": [
            {"panel_id": "p1", "decision": "accept", "view": "front", "iou": 0.95}]}
        self.assertEqual(self.client.post(f"/v1/forge/jobs/{job['id']}/review", json=incomplete).status_code, 422)
        self.assertEqual(self.store.get_job(job["id"])["state"], "review")
        job = self.stage(job)
        self.assertEqual(job["state"], "staged")
        approved = self.client.post(f"/v1/forge/jobs/{job['id']}/approve").json()
        self.assertEqual(approved["state"], "queued_bake")
        bake = self.client.post("/v1/forge/worker/claim", json={"kind": "pipeline"}).json()
        self.assertEqual(bake["state"], "baking")
        response = self.client.post("/v1/forge/worker/complete", json={
            "job_id": job["id"], "lease_id": bake["lease"]["lease_id"],
            "artifacts": {"model.glb": {"encoding": "base64", "data": base64.b64encode(b"glTF\x00").decode()},
                          "bake_report.json": {"iou": 0.98}}, "metrics": {"iou": 0.98}})
        self.assertEqual(response.status_code, 200, response.text)
        version = response.json()
        self.assertEqual(version["number"], 1)
        self.assertEqual(version["lineage"], {"parent_version": None, "root_version": 1})
        self.assertEqual(version["metrics"], {"part_layers": {}, "iou": 0.98})
        self.assertEqual(version["critic"], {"status": "pending"})
        self.assertEqual(self.client.get(f"/v1/forge/jobs/{job['id']}").json()["state"], "ready")
        self.assertEqual(self.client.get("/v1/forge/library").json()[0]["number"], 1)
        self.assertEqual(self.client.get("/v1/forge/status").json()["jobs_active"], 0)
        path = "/v1/forge/assets/chair/variants/oak/versions/1"
        self.assertEqual(self.client.get(path).json(), version)
        self.assertEqual(self.client.get(path + "/artifacts/model.glb").content, b"glTF\x00")
        self.assertEqual(self.client.get(path + "/layers").json(), {})

    def test_multipart_validation_leaves_no_jobs(self):
        good_file = ("files", ("sheet.png", png_bytes(), "image/png"))
        for params in ("invalid", "[]", "null", '{"iou":2}', '{"allow_extra":"yes"}', '{"unknown":0}', '{"iou":NaN}'):
            with self.subTest(params=params):
                response = self.client.post("/v1/forge/jobs", data={"asset": "chair", "variant": "oak", "params": params}, files=[good_file])
                self.assertEqual(response.status_code, 422, response.text)
        for files in ([], [("files", ("empty.png", b"", "image/png"))],
                      [("files", ("fake.png", b"fake", "image/png"))], [good_file] * 9):
            with self.subTest(files=len(files)):
                response = self.client.post("/v1/forge/jobs", data={"asset": "chair", "variant": "oak"}, files=files)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.list_jobs(), [])

    def test_eight_images_and_canonical_views_in_params(self):
        response = self.client.post("/v1/forge/jobs", data={"asset": "chair", "variant": "oak",
            "params": '{"canonical_views":["front"],"iou":0.8}'},
            files=[("files", (f"sheet{i}.png", png_bytes(), "image/png")) for i in range(8)])
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(response.json()["uploads"]), 8)
        self.assertEqual(response.json()["canonical_views"], ["front"])
        self.assertEqual(response.json()["params"]["iou"], 0.8)

    def test_uploads_and_panels_use_actual_image_type(self):
        job = self.upload()
        uploaded = self.client.get(f"/v1/forge/jobs/{job['id']}/uploads/0")
        self.assertEqual(uploaded.headers["content-type"], "image/png")
        self.assertEqual(uploaded.content, png_bytes())
        self.store.save_panel(job["id"], "p1", png_bytes())
        panel = self.client.get(f"/v1/forge/jobs/{job['id']}/panels/p1")
        self.assertEqual(panel.headers["content-type"], "image/png")
        self.assertEqual(panel.content, png_bytes())
        for suffix in ("uploads/9", "uploads/-1", "panels/%2e%2e", "panels/missing"):
            self.assertEqual(self.client.get(f"/v1/forge/jobs/{job['id']}/{suffix}").status_code, 404)

    def test_review_draft_missing_and_invalid_decisions(self):
        job = self.reviewing()
        path = f"/v1/forge/jobs/{job['id']}/review"
        panels = [{"panel_id": "p1", "decision": "accept", "view": "front"}]
        draft = self.client.post(path, json={"mode": "draft", "panels": panels})
        self.assertEqual(draft.json()["state"], "review")
        self.assertEqual(draft.json()["match"]["decisions"][0]["panel_id"], "p1")
        for body in ({"mode": "submit", "panels": panels, "views_missing": ["side"]},
                     {"mode": "submit", "panels": panels, "views_missing": ["front", "back"]},
                     {"mode": "submit", "panels": panels * 2, "views_missing": ["back"]},
                     {"mode": "submit", "panels": [{"panel_id": "p1", "decision": "reject", "view": "front"}], "views_missing": ["back"]}):
            self.assertEqual(self.client.post(path, json=body).status_code, 422)
        self.assertEqual(self.client.post(path, json={"mode": "submit", "panels": panels, "views_missing": ["back"]}).json()["state"], "staged")

    def test_empty_canonical_list_cannot_submit(self):
        job = self.upload(canonical_views="[]")
        self.progress(job, "matching")
        self.progress(job, "review")
        response = self.client.post(f"/v1/forge/jobs/{job['id']}/review", json={"mode": "submit", "panels": []})
        self.assertEqual(response.status_code, 422)

    def test_worker_auth_on_all_worker_mutations(self):
        job = self.upload()
        routes = [(f"/v1/forge/jobs/{job['id']}/progress", {"state": "matching"}),
                  ("/v1/forge/worker/claim", {"kind": "pipeline"}),
                  ("/v1/forge/worker/complete", {"job_id": job["id"]})]
        with patch.dict(os.environ, {"FORGE_WORKER_TOKEN": "test-worker-secret"}):
            for route, body in routes:
                for headers in ({}, {"X-Forge-Worker": "wrong"}):
                    self.assertEqual(self.client.post(route, json=body, headers=headers).status_code, 403)
            response = self.client.post(routes[1][0], json=routes[1][1], headers={"X-Forge-Worker": "test-worker-secret"})
            self.assertEqual(response.status_code, 200)

    def test_worker_heartbeat_and_stale_completion_rejection(self):
        job = self.baking()
        # Give the bake an expired lease by first returning to a fresh claimed job.
        another = self.upload()
        now = self.store._now()
        with patch.object(self.store, "_now", return_value=now):
            claim = self.client.post("/v1/forge/worker/claim", json={"kind": "pipeline"}).json()
        path = f"/v1/forge/jobs/{another['id']}/progress"
        lease = claim["lease"]["lease_id"]
        with patch.object(self.store, "_now", return_value=now + timedelta(seconds=10)):
            heartbeat = self.client.post(path, json={"lease_id": lease, "markers": ["alive"]})
        self.assertEqual(heartbeat.status_code, 200)
        self.assertGreater(heartbeat.json()["lease"]["lease_expires_at"], claim["lease"]["lease_expires_at"])
        with patch.object(self.store, "_now", return_value=now + timedelta(seconds=311)):
            reclaimed = self.client.post("/v1/forge/worker/claim", json={"kind": "pipeline"}).json()
            self.assertEqual(reclaimed["id"], another["id"])
            self.assertEqual(self.client.post(path, json={"state": "review", "lease_id": lease}).status_code, 409)
        self.assertEqual(self.client.post("/v1/forge/worker/complete", json={"job_id": job["id"], "lease_id": "stale"}).status_code, 409)

    def test_progress_cannot_bypass_review_approval_or_completion(self):
        job = self.reviewing()
        for state in ("staged", "queued_bake", "ready"):
            self.assertEqual(self.client.post(f"/v1/forge/jobs/{job['id']}/progress", json={"state": state}).status_code, 409)
        self.assertEqual(self.client.post(f"/v1/forge/jobs/{job['id']}/approve").status_code, 409)
        self.assertEqual(self.client.post("/v1/forge/worker/complete", json={"job_id": job["id"]}).status_code, 409)

    def test_delete_forbidden_while_baking(self):
        job = self.baking()
        path = f"/v1/forge/jobs/{job['id']}"
        self.assertEqual(self.client.delete(path).status_code, 409)
        failed = self.progress(job, "failed", error="bake failed")
        self.assertEqual(failed["error"], "bake failed")
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_artifact_confinement_and_nested_file_serve(self):
        _, version = self.completed()
        path = "/v1/forge/assets/chair/variants/oak/versions/1/artifacts/"
        self.assertEqual(self.client.get(path + "turntable/readme.txt").text, "frame notes")
        self.assertEqual(self.client.get(path + "bake_report.json").headers["content-type"], "application/json")
        for suffix in ("%2e%2e/version.json", "%2Fetc/passwd", "turntable/%2e%2e/%2e%2e/version.json", "missing"):
            self.assertEqual(self.client.get(path + suffix).status_code, 404, suffix)
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("private")
        directory = self.store._version_dir("chair", "oak", version["number"]) / "artifacts"
        (directory / "escape.txt").symlink_to(outside)
        self.assertEqual(self.client.get(path + "escape.txt").status_code, 404)

    def test_bad_completion_artifacts_do_not_publish_version(self):
        job = self.baking()
        for artifacts in ({"../escape": "bad"}, {"/absolute": "bad"},
                          {"bad.glb": {"encoding": "base64", "data": "!!!"}},
                          {"bad.json": "not json"}, {"a": "x", "a/b": "y"}):
            response = self.client.post("/v1/forge/worker/complete", json={"job_id": job["id"], "artifacts": artifacts})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.store.list_versions(), [])
            self.assertEqual(self.store.get_job(job["id"])["state"], "baking")

    def test_accept_notes_and_library_filters(self):
        job, _ = self.completed()
        path = "/v1/forge/assets/chair/variants/oak/versions/1"
        self.assertTrue(self.client.post(path + "/accept").json()["accepted"])
        self.assertTrue(self.client.get(f"/v1/forge/jobs/{job['id']}").json()["accepted"])
        note = self.client.post(path + "/notes", json={"author": "owner", "text": "Polish the legs"})
        self.assertEqual(note.status_code, 200)
        self.assertEqual(note.json()["notes"][0]["author"], "owner")
        self.assertEqual(self.client.post(path + "/notes", json={"author": "owner", "text": " "}).status_code, 422)
        library = self.client.get("/v1/forge/library", params={"asset": "chair", "origin": "bake", "accepted": "true", "q": "polish"}).json()
        self.assertEqual(len(library), 1)
        self.assertEqual(self.client.get("/v1/forge/library?accepted=false").json(), [])
        self.assertEqual(self.client.get("/v1/forge/library?origin=trellis").json(), [])
        self.assertEqual(self.client.get("/v1/forge/assets/chair/variants/oak/versions").json(), library)
        self.assertFalse(self.client.post(path + "/accept", json={"accepted": False}).json()["accepted"])

    def test_iteration_creates_new_child_and_jobs_filters(self):
        parent, _ = self.completed()
        child = self.upload(intent="iterate_params", parent_job=parent["id"], parent_version="1")
        self.assertNotEqual(parent["id"], child["id"])
        self.assertEqual(child["parent_version"], 1)
        self.assertEqual(child["parent_job"], parent["id"])
        self.assertEqual(self.client.get("/v1/forge/jobs?state=uploaded&asset=chair").json(),
                         [{**child, "attention": None, "policy": {"mode": "off"}}])
        self.assertEqual(self.client.get("/v1/forge/jobs?asset=missing").json(), [])
        self.assertEqual(self.client.get("/v1/forge/assets").json(), [{"asset": "chair", "variant": "oak"}])

    def test_upload_failure_rolls_back_job(self):
        with patch.object(self.store, "record_upload", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.upload()
        self.assertEqual(self.store.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
