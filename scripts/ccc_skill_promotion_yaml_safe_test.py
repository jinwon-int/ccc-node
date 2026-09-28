"""YAML-safe SKILL.md description lines in the promotion publisher (#2032).

fleet-skills validate.py (fleet-skills#328) parses frontmatter with PyYAML and
rejects unquoted descriptions YAML reads differently (``": "`` makes the line
invalid, ``" #"`` truncates it). The publisher must:

* decode a quoted description everywhere it reads one (no quotes leaking into
  lengths, envelopes, dedup, or the inventory snapshot);
* refuse, node-side, to snapshot a skill whose description line is unsafe
  (``skill_description_yaml_unsafe``) and let the deterministic autorepair
  re-render it quoted;
* re-render the description line of reviser output before republishing.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location(
    "promotion_yaml_safe_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

try:
    import yaml
except ImportError:  # PyYAML is optional on nodes; the parse checks skip.
    yaml = None

UNSAFE = "Use when a Cloudflare token leaked. Triggers: cache purge, token rotation #42"
QUOTED = json.dumps(UNSAFE)  # a JSON string is also a valid YAML double-quoted scalar


def skill_md(name: str, description_line: str) -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description_line}\n"
        "---\n"
        f"# {name}\n"
        "\n"
        "Documented skill body for the YAML-safe fixture.\n"
    ).encode()


def yaml_frontmatter(payload: bytes) -> dict:
    lines = payload.decode("utf-8").splitlines()
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end]))


def envelope(name: str, content: bytes, description: str) -> dict:
    files = [{"path": "SKILL.md", "content_b64": base64.b64encode(content).decode(),
              "executable": False}]
    skill_sha = hashlib.sha256(content).hexdigest()
    digest = hashlib.sha256()
    digest.update(b"SKILL.md\0")
    digest.update(skill_sha.encode())
    digest.update(b"\0")
    tree_sha = digest.hexdigest()
    return {
        "schema_version": 1,
        "transport_id": f"node-a-claude-{name}-{tree_sha[:12]}",
        "created_at": "2026-09-28T00:00:00Z",
        "node": "node-a",
        "provider": "claude",
        "name": name,
        "description": description,
        "skill_sha256": skill_sha,
        "tree_sha256": tree_sha,
        "files": files,
    }


class FrontmatterReaderTests(unittest.TestCase):
    def test_quoted_description_is_decoded(self):
        payload = skill_md("demo-skill", QUOTED)
        self.assertEqual(promotion._frontmatter(payload, "demo-skill"), UNSAFE)
        self.assertEqual(
            promotion._frontmatter(payload, "demo-skill", require_yaml_safe=True), UNSAFE)

    def test_unsafe_unquoted_description_is_refused_node_side_only(self):
        payload = skill_md("demo-skill", UNSAFE)
        # Lenient read (envelopes from not-yet-updated nodes) keeps working...
        self.assertEqual(promotion._frontmatter(payload, "demo-skill"), UNSAFE)
        # ...but the node-side snapshot gate refuses it for autorepair.
        with self.assertRaises(promotion.PromotionError) as caught:
            promotion._frontmatter(payload, "demo-skill", require_yaml_safe=True)
        self.assertEqual(caught.exception.code, "skill_description_yaml_unsafe")
        self.assertIn("skill_description_yaml_unsafe", promotion._AUTOREPAIR_CODES)

    def test_plain_safe_description_passes_the_gate(self):
        plain = "Use when validating a plain description that YAML reads verbatim"
        payload = skill_md("demo-skill", plain)
        self.assertEqual(
            promotion._frontmatter(payload, "demo-skill", require_yaml_safe=True), plain)

    def test_quoting_counts_toward_the_raw_length_cap(self):
        # fleet-skills checks both parsed and raw length against 1024.
        near_cap = "Use when x: " + "y" * 1011
        self.assertEqual(len(near_cap), 1023)  # 1025 once quoted
        payload = skill_md("demo-skill", json.dumps(near_cap))
        with self.assertRaises(promotion.PromotionError) as caught:
            promotion._frontmatter(payload, "demo-skill")
        self.assertEqual(caught.exception.code, "skill_frontmatter_invalid")

    def test_central_frontmatter_decodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_bytes(skill_md("demo-skill", QUOTED))
            self.assertEqual(promotion._central_frontmatter(path), ("demo-skill", UNSAFE))

    def test_envelope_accepts_decoded_and_legacy_raw_description(self):
        content = skill_md("demo-skill", QUOTED)
        for recorded in (UNSAFE, QUOTED):
            candidate, _, _ = promotion._candidate_from_envelope(
                envelope("demo-skill", content, recorded))
            self.assertEqual(candidate.description, UNSAFE)
        with self.assertRaises(promotion.PromotionError) as caught:
            promotion._candidate_from_envelope(
                envelope("demo-skill", content, "a different description entirely"))
        self.assertEqual(caught.exception.code, "envelope_skill_mismatch")


class WriterTests(unittest.TestCase):
    def _skill_dir(self, root: Path, description_line: str) -> Path:
        skill_dir = root / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(skill_md("demo-skill", description_line))
        (skill_dir / ".autosave-meta.json").write_text(
            json.dumps({"skill_sha256": "old"}), encoding="utf-8")
        return skill_dir

    def test_repair_rerenders_unsafe_description_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._skill_dir(Path(tmp), UNSAFE)
            promotion._repair_skill_frontmatter(skill_dir, "demo-skill")
            payload = (skill_dir / "SKILL.md").read_bytes()
            self.assertIn(f"description: {QUOTED}\n".encode(), payload)
            self.assertEqual(
                promotion._frontmatter(payload, "demo-skill", require_yaml_safe=True), UNSAFE)
            if yaml is not None:
                self.assertEqual(yaml_frontmatter(payload),
                                 {"name": "demo-skill", "description": UNSAFE})
            # Idempotent: a second repair leaves the bytes alone.
            promotion._repair_skill_frontmatter(skill_dir, "demo-skill")
            self.assertEqual((skill_dir / "SKILL.md").read_bytes(), payload)

    def test_autorepair_handles_the_yaml_code_and_restamps_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = self._skill_dir(root, UNSAFE)
            config = SimpleNamespace(provider_roots={"claude": root}, review_llm_cmd=())
            self.assertTrue(promotion._autorepair_candidate(
                config, "claude", "demo-skill", "skill_description_yaml_unsafe"))
            payload = (skill_dir / "SKILL.md").read_bytes()
            marker = json.loads((skill_dir / ".autosave-meta.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["skill_sha256"], hashlib.sha256(payload).hexdigest())
            self.assertTrue(promotion._frontmatter(payload, "demo-skill", require_yaml_safe=True))

    def test_revised_files_are_rerendered_before_republish(self):
        revised = [("SKILL.md", skill_md("demo-skill", UNSAFE))]
        candidate = promotion._candidate_from_revised_files(
            "node-a", "claude", "demo-skill", revised, {})
        payload = candidate.files[0].content
        self.assertIn(f"description: {QUOTED}\n".encode(), payload)
        self.assertEqual(candidate.description, UNSAFE)
        self.assertEqual(candidate.skill_sha256, hashlib.sha256(payload).hexdigest())
        if yaml is not None:
            self.assertEqual(yaml_frontmatter(payload)["description"], UNSAFE)


if __name__ == "__main__":
    unittest.main()
