"""Public dependency records round trip without storing prompts, answers or framework metadata."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.dependencies import AnswerDependencies, ChunkDependency, MAX_CHUNKS
from setrya.drill import InputError
from setrya.store_signals import passage_metadata


SCOPE = dict(answer_id="answer-1", tenant="t1", access_scope="support", access_version="42",
             retrieval_scope="refund", retrieval_version="7")


class AnswerDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.first = passage_metadata("policy", "A", "0")
        self.second = passage_metadata("policy", "B", "0")
        context = [dict(self.second, text="private passage"), dict(self.first, query="private query"), self.second]
        self.record = AnswerDependencies.record(**SCOPE, chunks=iter(context))

    def test_actual_sources_are_immutable_ordered_and_text_free(self):
        self.assertEqual(self.record.markers,
                         (self.second["setrya_marker"], self.first["setrya_marker"], self.second["setrya_marker"]))
        self.assertEqual(self.record.to_dict()["chunks"], [self.second, self.first, self.second])
        with self.assertRaises(FrozenInstanceError):
            self.record.chunks[0].document = "another"
        with TemporaryDirectory() as temp:
            path = Path(temp) / "records/answer.json"
            self.record.save(path)
            raw = path.read_text()
            self.assertNotIn("private passage", raw)
            self.assertNotIn("private query", raw)
            self.assertEqual(AnswerDependencies.load(path), self.record)
            self.assertEqual(AnswerDependencies.from_dict(json.loads(raw)), self.record)
            empty = AnswerDependencies.record(**(SCOPE | {"retrieval_version": "8"}), chunks=[])
            empty.save(path)
            self.assertEqual(AnswerDependencies.load(path), empty)
            self.assertEqual(empty.markers, ())

    def test_invalid_or_unordered_input_does_not_declare_a_valid_dependency(self):
        for bad in ({"document": "unused"}, {**self.first, "revision": "B"}, {**self.second, "chunk": ""}, None):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                AnswerDependencies.record(**SCOPE, chunks=[bad])
        for bad in ("", b"", {}, set(), None, [self.first] * (MAX_CHUNKS + 1)):
            with self.subTest(kind=type(bad)), self.assertRaises(InputError):
                AnswerDependencies.record(**SCOPE, chunks=bad)
        with self.assertRaises(InputError):
            AnswerDependencies(**SCOPE, chunks=[ChunkDependency("doc", "A", "0")])
        for key in SCOPE:
            with self.subTest(key=key), self.assertRaises(InputError):
                AnswerDependencies.record(**(SCOPE | {key: "a free text field"}), chunks=[])

    def test_reader_requires_v1_exact_keys_and_matching_markers(self):
        valid = self.record.to_dict()
        bad_inputs = [valid | {key: val} for key, val in (("format", "other"), ("version", True),
                      ("version", 2), ("chunks", {}), ("raw_prompt", "hidden"))]
        for key in SCOPE:
            bad_inputs.append({k: v for k, v in valid.items() if k != key})
        for change in ({"setrya_marker": "another"}, {"raw_text": "private"}):
            bad = deepcopy(valid)
            bad["chunks"][0].update(change)
            bad_inputs.append(bad)
        for i, value in enumerate(bad_inputs):
            with self.subTest(case=i), self.assertRaises(InputError):
                AnswerDependencies.from_dict(value)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            for raw in ('{"version": 1, "version": 1}', "[" * 1500, "x" * (1024 * 1024 + 1)):
                path.write_text(raw)
                with self.assertRaises(InputError):
                    AnswerDependencies.load(path)
