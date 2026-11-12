import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.parent = self.base / "exports"
        self.source.mkdir()
        self.parent.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_file(self, relative, data=b"original"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_report(self, exported):
        with open(exported["report_path"], encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_full_scan_statuses_preserves_line_numbers_and_zeroes(self):
        self.make_file("a/001.pdf")
        self.make_file("b/double.txt")
        self.make_file("c/double.txt")
        result = core.scan(str(self.source), "\n001.pdf\n\nmissing.txt\ndouble.txt\n001.PDF\n../escape\nfoo*\n")
        self.assertTrue(result["complete"])
        self.assertEqual(result["scanned"], 3)
        self.assertEqual([row["line"] for row in result["rows"]], [2, 4, 5, 6, 7, 8])
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["unique", "missing", "ambiguous", "duplicate", "invalid", "invalid"])
        self.assertEqual(result["rows"][0]["query"], "001.pdf")

    def test_stem_never_strips_query_suffix(self):
        self.make_file("archive.tar.gz")
        result = core.scan(str(self.source), "archive.tar\narchive\narchive.tar.gz", mode="stem")
        self.assertEqual([row["status"] for row in result["rows"]], ["unique", "missing", "missing"])

    def test_case_sensitive_and_nonrecursive(self):
        self.make_file("Case.txt")
        self.make_file("nested/item.txt")
        result = core.scan(str(self.source), "case.txt\nCase.txt\nitem.txt", recursive=False, case_sensitive=True)
        self.assertEqual(result["scanned"], 1)
        self.assertEqual([row["status"] for row in result["rows"]], ["missing", "unique", "missing"])

    def test_full_names_do_not_expand_sharp_s_into_two_letters(self):
        sharp_s = self.make_file("Straße.pdf", b"sharp-s file")
        missing = core.scan(str(self.source), "STRASSE.pdf")
        self.assertEqual(missing["rows"][0]["status"], "missing")
        self.assertEqual(core.export_result(missing, str(self.parent))["copied"], 0)

        double_s = self.make_file("STRASSE.pdf", b"double-s file")
        result = core.scan(str(self.source), "STRASSE.pdf\nSTRAẞE.pdf\nstraße.pdf")
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["unique", "unique", "duplicate"])
        self.assertEqual(result["rows"][0]["candidates"], [str(double_s)])
        self.assertEqual(result["rows"][1]["candidates"], [str(sharp_s)])
        exported = core.export_result(result, str(self.parent))
        output = Path(exported["output_dir"])
        self.assertEqual(exported["copied"], 2)
        self.assertEqual((output / sharp_s.name).read_bytes(), b"sharp-s file")
        self.assertEqual((output / double_s.name).read_bytes(), b"double-s file")
        sensitive = core.scan(str(self.source), "STRAẞE.pdf\nStraße.pdf\nSTRASSE.pdf",
                              case_sensitive=True)
        self.assertEqual([row["status"] for row in sensitive["rows"]],
                         ["missing", "unique", "unique"])

    def test_stem_names_keep_sharp_s_separate_and_match_capital_sharp_s(self):
        sharp_s = self.make_file("Straße.pdf")
        double_s = self.make_file("STRASSE.txt")
        result = core.scan(str(self.source), "STRASSE\nSTRAẞE", mode="stem")
        self.assertEqual([row["status"] for row in result["rows"]], ["unique", "unique"])
        self.assertEqual([row["candidates"] for row in result["rows"]],
                         [[str(double_s)], [str(sharp_s)]])

    def test_invalid_names_and_no_implicit_whitespace_trimming(self):
        self.make_file(" space.txt")
        result = core.scan(str(self.source), "CON\nNUL.txt\nfoo\tbar\nname.\nname \n space.txt")
        self.assertEqual([row["status"] for row in result["rows"]], ["invalid"] * 5 + ["unique"])

    def test_cancelled_scan_cannot_copy_but_can_export_report(self):
        self.make_file("one.txt")
        event = threading.Event()
        event.set()
        result = core.scan(str(self.source), "one.txt\nmissing", cancel_event=event)
        self.assertFalse(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]], ["uncertain", "uncertain"])
        with self.assertRaises(ValueError):
            core.export_result(result, str(self.parent))
        exported = core.export_result(result, str(self.parent), copy_files=False)
        self.assertTrue(Path(exported["report_path"]).exists())
        self.assertEqual(exported["copied"], 0)

    def test_permission_error_does_not_make_a_false_unique(self):
        self.make_file("visible/same.txt")
        blocked = self.source / "blocked"
        blocked.mkdir()
        actual_scandir = os.scandir
        def guarded(path):
            if Path(path) == blocked:
                raise PermissionError("test inaccessible folder")
            return actual_scandir(path)
        with mock.patch.object(core.os, "scandir", side_effect=guarded):
            result = core.scan(str(self.source), "same.txt\nmissing.txt")
        self.assertFalse(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]], ["uncertain", "uncertain"])
        self.assertTrue(any("test inaccessible" in warning for warning in result["warnings"]))

    def test_copy_preserves_relative_paths_source_and_excludes_ambiguous(self):
        source_file = self.make_file("nested/001.pdf", b"payload")
        self.make_file("one/a.txt")
        self.make_file("two/a.txt")
        result = core.scan(str(self.source), "001.pdf\na.txt\nmissing\n001.pdf")
        exported = core.export_result(result, str(self.parent))
        output = Path(exported["output_dir"])
        self.assertEqual(exported["copied"], 1)
        self.assertEqual(exported["failed"], 0)
        self.assertEqual((output / "nested/001.pdf").read_bytes(), b"payload")
        self.assertEqual(source_file.read_bytes(), b"payload")
        self.assertFalse((output / "one/a.txt").exists())
        self.assertEqual(Path(exported["report_path"]).read_bytes()[:3], b"\xef\xbb\xbf")
        self.assertEqual(len([row for row in self.read_report(exported) if row["记录类型"] == "清单项"]), 4)
        second = core.export_result(result, str(self.parent))
        self.assertNotEqual(exported["output_dir"], second["output_dir"])

    def test_output_inside_source_is_rejected_before_any_output(self):
        self.make_file("file.txt")
        result = core.scan(str(self.source), "file.txt")
        with self.assertRaises(ValueError):
            core.export_result(result, str(self.source))
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), ["file.txt"])

    def test_tampered_candidate_outside_root_is_never_copied(self):
        outside = self.base / "private.txt"
        outside.write_text("private")
        result = core.scan(str(self.source), "file.txt")
        result["rows"][0].update(status="unique", candidates=[str(outside)])
        exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["copied"], 0)
        self.assertEqual(exported["failed"], 1)
        self.assertFalse((Path(exported["output_dir"]) / "private.txt").exists())

    def test_missing_source_since_preview_is_reported(self):
        file = self.make_file("gone.txt")
        result = core.scan(str(self.source), "gone.txt")
        file.unlink()
        exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["failed"], 1)
        self.assertEqual(self.read_report(exported)[1]["导出状态"], "复制失败")

    def test_existing_destination_is_never_overwritten(self):
        self.make_file("same.txt", b"source")
        output = self.parent / "fixed-output"
        output.mkdir()
        (output / "same.txt").write_bytes(b"do not overwrite")
        result = core.scan(str(self.source), "same.txt")
        with mock.patch.object(core.tempfile, "mkdtemp", return_value=str(output)):
            exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["failed"], 1)
        self.assertEqual((output / "same.txt").read_bytes(), b"do not overwrite")

    def test_csv_formula_injection_is_neutralized(self):
        self.make_file("=1+1.txt")
        self.make_file("+plus.txt")
        result = core.scan(str(self.source), "=1+1.txt\n+plus.txt\n@missing")
        exported = core.export_result(result, str(self.parent), copy_files=False)
        rows = self.read_report(exported)
        values = [row["清单文件名"] for row in rows if row["记录类型"] == "清单项"]
        self.assertEqual(values, ["'=1+1.txt", "'+plus.txt", "'@missing"])

    def test_csv_numeric_names_are_text_protected_without_changing_paths_or_line_numbers(self):
        names = ["000123", "12345678901234567890"]
        files = [self.make_file(name) for name in names]
        result = core.scan(str(self.source), "\n" + "\n".join(names))
        exported = core.export_result(result, str(self.parent))
        rows = self.read_report(exported)[1:]
        self.assertEqual([row["query"] for row in result["rows"]], names)
        self.assertEqual([row["清单文件名"] for row in rows], ["'" + name for name in names])
        self.assertEqual([row["清单行号"] for row in rows], ["2", "3"])
        self.assertEqual([row["候选文件路径"] for row in rows], [str(path) for path in files])
        self.assertEqual([row["实际选择路径"] for row in rows], [str(path) for path in files])
        self.assertEqual(exported["copied"], 2)
        for name in names:
            self.assertEqual((Path(exported["output_dir"]) / name).read_bytes(), b"original")

    def test_cancelled_export_records_unstarted_items(self):
        self.make_file("one.txt")
        self.make_file("two.txt")
        event = threading.Event()
        result = core.scan(str(self.source), "one.txt\ntwo.txt")
        def progress(message):
            if "正在复制" in message:
                event.set()
        exported = core.export_result(result, str(self.parent), cancel_event=event, progress=progress)
        self.assertTrue(exported["cancelled"])
        self.assertEqual(exported["copied"], 0)
        self.assertEqual(exported["failed"], 0)
        self.assertEqual([row["导出状态"] for row in self.read_report(exported) if row["记录类型"] == "清单项"],
                         ["已取消", "已取消"])

    def test_symlink_is_skipped_and_export_rechecks_links(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"private")
        link = self.source / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is unavailable without Windows privilege")
        result = core.scan(str(self.source), "link.txt")
        self.assertEqual(result["rows"][0]["status"], "missing")
        self.assertTrue(result["warnings"])
        result["rows"][0].update(status="unique", candidates=[str(link)])
        exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["failed"], 1)
        self.assertEqual(exported["copied"], 0)

    def test_reparse_file_is_excluded_without_needing_symlink_privilege(self):
        path = self.make_file("cloud.txt")
        real = core._is_reparse
        def pretend_reparse(candidate, info=None):
            return Path(candidate) == path or real(candidate, info)
        with mock.patch.object(core, "_is_reparse", side_effect=pretend_reparse):
            result = core.scan(str(self.source), "cloud.txt")
        self.assertTrue(result["complete"])
        self.assertEqual(result["rows"][0]["status"], "missing")
        self.assertTrue(result["warnings"])

    def test_partial_copy_failure_is_preserved_and_reported(self):
        self.make_file("large.bin", b"12345678")
        result = core.scan(str(self.source), "large.bin")
        def fail_after_partial(candidate, root, output, event):
            target = output / "large.bin"
            with open(target, "xb") as stream:
                stream.write(b"123")
            raise OSError("simulated disk full")
        with mock.patch.object(core, "_copy_exclusive", side_effect=fail_after_partial):
            exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["failed"], 1)
        self.assertEqual((Path(exported["output_dir"]) / "large.bin").read_bytes(), b"123")
        self.assertIn("simulated disk full", self.read_report(exported)[1]["导出说明"])

    def test_source_report_filename_does_not_collide_with_generated_report(self):
        self.make_file("核对报告.csv", b"source CSV content")
        result = core.scan(str(self.source), "核对报告.csv")
        exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["copied"], 1)
        self.assertEqual(exported["report_error"], "")
        self.assertEqual(Path(exported["report_path"]).name, "核对报告-2.csv")
        self.assertEqual((Path(exported["output_dir"]) / "核对报告.csv").read_bytes(), b"source CSV content")

    def test_report_write_failure_is_explicit_and_does_not_lose_copies(self):
        self.make_file("one.txt", b"saved")
        result = core.scan(str(self.source), "one.txt")
        actual_open = open
        def deny_report(path, mode="r", *args, **kwargs):
            if mode == "x":
                raise PermissionError("test report write denied")
            return actual_open(path, mode, *args, **kwargs)
        with mock.patch("core.open", side_effect=deny_report, create=True):
            exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["copied"], 1)
        self.assertEqual(exported["report_path"], "")
        self.assertIn("test report write denied", exported["report_error"])
        self.assertEqual((Path(exported["output_dir"]) / "one.txt").read_bytes(), b"saved")

    def test_csv_uses_chinese_labels_without_changing_api_statuses(self):
        self.make_file("one.txt")
        self.make_file("a/two.txt")
        self.make_file("b/two.txt")
        result = core.scan(str(self.source), "one\nmissing\ntwo\none\n../invalid", mode="stem")
        result["warnings"].append("测试警告")
        exported = core.export_result(result, str(self.parent))
        rows = self.read_report(exported)
        self.assertEqual(rows[0]["记录类型"], "汇总")
        self.assertIn("匹配模式：不含扩展名", rows[0]["匹配说明"])
        self.assertEqual(rows[0]["匹配状态"], "扫描完整")
        self.assertEqual(rows[0]["导出状态"], "处理结束")
        self.assertEqual([row["匹配状态"] for row in rows if row["记录类型"] == "清单项"],
                         ["唯一匹配", "未找到", "多份匹配", "清单重复", "输入无效"])
        self.assertEqual(rows[1]["导出状态"], "复制成功")
        self.assertEqual(rows[2]["导出状态"], "未复制")
        self.assertEqual(rows[-1]["记录类型"], "警告")
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["unique", "missing", "ambiguous", "duplicate", "invalid"])

    def test_output_timestamp_uses_shanghai_even_when_host_time_is_utc(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                reference = datetime(2026, 11, 12, 20, 5, 6, tzinfo=timezone.utc)
                return reference.astimezone(tz) if tz is not None else reference.replace(tzinfo=None)
        result = core.scan(str(self.source), "missing.txt")
        with mock.patch.object(core, "datetime", FrozenDatetime):
            exported = core.export_result(result, str(self.parent), copy_files=False)
        self.assertTrue(Path(exported["output_dir"]).name.startswith("清单取件-20261113-040506-"))
        self.assertEqual(self.read_report(exported)[1]["导出状态"], "仅导出报告")
        self.assertIn("匹配模式：完整文件名", self.read_report(exported)[0]["匹配说明"])

    def test_manual_choice_copies_only_chosen_candidate_and_keeps_trace(self):
        first = self.make_file("older/same.txt", b"first original")
        chosen = self.make_file("newer/same.txt", b"chosen original")
        result = core.scan(str(self.source), "same.txt")
        row = result["rows"][0]
        original_candidates = list(row["candidates"])
        self.assertEqual(row["status"], "ambiguous")
        row.update(status="selected", selected_candidate=str(chosen),
                   reason="用户已手动选择一份；其余候选未复制。")
        exported = core.export_result(result, str(self.parent))
        output = Path(exported["output_dir"])
        self.assertEqual((exported["copied"], exported["failed"]), (1, 0))
        self.assertEqual((output / "newer/same.txt").read_bytes(), b"chosen original")
        self.assertFalse((output / "older/same.txt").exists())
        self.assertEqual(first.read_bytes(), b"first original")
        self.assertEqual(chosen.read_bytes(), b"chosen original")
        self.assertEqual(row["candidates"], original_candidates)
        report = self.read_report(exported)[1]
        self.assertEqual(report["匹配状态"], "人工选择")
        self.assertEqual(report["候选文件路径"].splitlines(), original_candidates)
        self.assertEqual(report["实际选择路径"], str(chosen))
        self.assertEqual(report["目标路径"], str(output / "newer/same.txt"))
        self.assertEqual(report["导出状态"], "复制成功")

    def test_manual_choice_outside_candidate_list_cannot_copy(self):
        self.make_file("a/same.txt")
        self.make_file("b/same.txt")
        outside = self.base / "outside.txt"
        outside.write_bytes(b"private")
        result = core.scan(str(self.source), "same.txt")
        result["rows"][0].update(status="selected", selected_candidate=str(outside))
        with mock.patch.object(core, "_copy_exclusive") as copy:
            exported = core.export_result(result, str(self.parent))
        copy.assert_not_called()
        self.assertEqual((exported["copied"], exported["failed"]), (0, 1))
        self.assertIn("不属于原候选列表", self.read_report(exported)[1]["导出说明"])
        self.assertFalse((Path(exported["output_dir"]) / "outside.txt").exists())

    def test_tampered_manual_choice_inside_candidates_still_cannot_escape_root(self):
        first = self.make_file("a/same.txt")
        outside = self.base / "private.txt"
        outside.write_bytes(b"private")
        result = core.scan(str(self.source), "same.txt")
        result["rows"][0].update(status="selected", candidates=[str(first), str(outside)],
                                 selected_candidate=str(outside))
        exported = core.export_result(result, str(self.parent))
        self.assertEqual((exported["copied"], exported["failed"]), (0, 1))
        self.assertIn("不在来源目录内", self.read_report(exported)[1]["导出说明"])

    def test_selected_file_disappearing_does_not_fall_back_to_other_candidate(self):
        remaining = self.make_file("a/same.txt", b"remaining")
        selected = self.make_file("b/same.txt", b"selected")
        result = core.scan(str(self.source), "same.txt")
        result["rows"][0].update(status="selected", selected_candidate=str(selected))
        selected.unlink()
        exported = core.export_result(result, str(self.parent))
        self.assertEqual((exported["copied"], exported["failed"]), (0, 1))
        self.assertFalse((Path(exported["output_dir"]) / "a/same.txt").exists())
        self.assertEqual(remaining.read_bytes(), b"remaining")
        self.assertEqual(self.read_report(exported)[1]["实际选择路径"], str(selected))

    def test_ambiguous_without_selection_stays_uncopied(self):
        self.make_file("a/same.txt")
        self.make_file("b/same.txt")
        result = core.scan(str(self.source), "same.txt")
        exported = core.export_result(result, str(self.parent))
        self.assertEqual((exported["copied"], exported["failed"]), (0, 0))
        self.assertEqual(list(Path(exported["output_dir"]).rglob("*.txt")), [])
        report = self.read_report(exported)[1]
        self.assertEqual(report["匹配状态"], "多份匹配")
        self.assertEqual(report["实际选择路径"], "")
        self.assertEqual(report["导出状态"], "未复制")

    def test_manual_choice_requires_path_and_at_least_two_candidates(self):
        first = self.make_file("a/same.txt")
        second = self.make_file("b/same.txt")
        cases = [
            ([str(first), str(second)], None),
            ([str(first)], str(first)),
            ([str(first), str(second)], "a/same.txt"),
        ]
        for candidates, selected in cases:
            with self.subTest(candidates=candidates, selected=selected):
                result = core.scan(str(self.source), "same.txt")
                row = result["rows"][0]
                row.update(status="selected", candidates=candidates)
                if selected is not None:
                    row["selected_candidate"] = selected
                with mock.patch.object(core, "_copy_exclusive") as copy:
                    exported = core.export_result(result, str(self.parent))
                copy.assert_not_called()
                self.assertEqual((exported["copied"], exported["failed"]), (0, 1))
                self.assertEqual(self.read_report(exported)[1]["导出状态"], "复制失败")

    def test_cancelled_scan_with_forged_manual_selection_still_cannot_copy(self):
        first = self.make_file("a/same.txt")
        second = self.make_file("b/same.txt")
        cancelled = threading.Event()
        cancelled.set()
        result = core.scan(str(self.source), "same.txt", cancel_event=cancelled)
        result["rows"][0].update(status="selected", candidates=[str(first), str(second)],
                                 selected_candidate=str(second))
        with self.assertRaisesRegex(ValueError, "扫描未完成"):
            core.export_result(result, str(self.parent))
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_invalid_manual_choice_is_explicit_in_report_only_export(self):
        self.make_file("a/same.txt")
        self.make_file("b/same.txt")
        result = core.scan(str(self.source), "same.txt")
        result["rows"][0]["status"] = "selected"
        exported = core.export_result(result, str(self.parent), copy_files=False)
        self.assertEqual((exported["copied"], exported["failed"]), (0, 1))
        self.assertEqual(self.read_report(exported)[1]["导出状态"], "复制失败")
        self.assertIn("缺少有效", self.read_report(exported)[1]["导出说明"])

    def test_cancelled_export_does_not_copy_a_valid_manual_selection(self):
        first = self.make_file("a/same.txt")
        second = self.make_file("b/same.txt")
        result = core.scan(str(self.source), "same.txt")
        result["rows"][0].update(status="selected", selected_candidate=str(second))
        cancelled = threading.Event()
        cancelled.set()
        exported = core.export_result(result, str(self.parent), cancel_event=cancelled)
        self.assertEqual((exported["copied"], exported["failed"]), (0, 0))
        self.assertTrue(exported["cancelled"])
        self.assertEqual(self.read_report(exported)[1]["导出状态"], "已取消")
        self.assertEqual(first.read_bytes(), b"original")
        self.assertEqual(second.read_bytes(), b"original")

    def test_regex_searches_full_filename_and_respects_anchors_and_case(self):
        self.make_file("nested/prefix-Invoice-001.PDF")
        self.make_file("other.txt")
        result = core.scan(str(self.source), r"invoice-\d+\.pdf$" + "\n" + r"^invoice", mode="regex")
        self.assertTrue(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]], ["unique", "missing"])
        sensitive = core.scan(str(self.source), r"invoice", mode="regex", case_sensitive=True)
        self.assertEqual(sensitive["rows"][0]["status"], "missing")

    def test_regex_opposite_escapes_are_not_casefolded_as_duplicates(self):
        self.make_file("123.pdf")
        self.make_file("abc.pdf")
        result = core.scan(str(self.source), r"^\d" + "\n" + r"^\D", mode="regex")
        self.assertEqual([row["status"] for row in result["rows"]], ["unique", "unique"])
        self.assertEqual([Path(row["candidates"][0]).name for row in result["rows"]], ["123.pdf", "abc.pdf"])

    def test_invalid_regex_blocks_copy_and_reports_the_bad_rule(self):
        self.make_file("one.pdf")
        result = core.scan(str(self.source), "[\none", mode="regex")
        self.assertFalse(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]], ["invalid", "uncertain"])
        self.assertTrue(result["warnings"])
        with self.assertRaisesRegex(ValueError, "扫描未完成"):
            core.export_result(result, str(self.parent))
        exported = core.export_result(result, str(self.parent), copy_files=False)
        report = self.read_report(exported)
        self.assertEqual(report[1]["匹配状态"], "输入无效")
        self.assertIn("正则表达式无效", report[1]["匹配说明"])

    def test_real_regex_backtracking_times_out_without_blocking_the_scan(self):
        self.make_file("a" * 180 + "!")
        started = time.monotonic()
        result = core.scan(str(self.source), r"^(a|aa)+$", mode="regex")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["rows"][0]["status"], "invalid")
        self.assertIn("超过 0.05 秒", result["rows"][0]["reason"])
        self.assertTrue(any("已停止使用此规则" in warning for warning in result["warnings"]))

    def test_timed_out_regex_is_disabled_and_every_match_uses_a_timeout(self):
        self.make_file("first.txt")
        self.make_file("second.txt")
        fake_pattern = mock.Mock()
        fake_pattern.search.side_effect = TimeoutError("test timeout")
        with mock.patch.object(core.regex_engine, "compile", return_value=fake_pattern):
            result = core.scan(str(self.source), ".*", mode="regex")
        self.assertEqual(fake_pattern.search.call_count, 1)
        self.assertEqual(fake_pattern.search.call_args.kwargs["timeout"], 0.05)
        self.assertFalse(result["complete"])
        self.assertEqual(result["scanned"], 2)

    def test_extension_normalization_final_suffix_and_no_extension(self):
        first = self.make_file("one.PDF")
        second = self.make_file("nested/two.pdf")
        no_suffix = self.make_file("README")
        archive = self.make_file("archive.tar.gz")
        result = core.scan(str(self.source), "pdf\n.PDF\n*.pdf\n[无后缀]\ngz", mode="extension")
        self.assertTrue(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["multiple", "duplicate", "duplicate", "unique", "unique"])
        self.assertEqual(set(result["rows"][0]["candidates"]), {str(first), str(second)})
        self.assertEqual(result["rows"][3]["candidates"], [str(no_suffix)])
        self.assertEqual(result["rows"][4]["candidates"], [str(archive)])

    def test_extension_rules_and_filters_do_not_expand_sharp_s(self):
        sharp_s = self.make_file("first.ß")
        double_s = self.make_file("second.ss")
        result = core.scan(str(self.source), "ẞ\nss", mode="extension")
        self.assertEqual([row["status"] for row in result["rows"]], ["unique", "unique"])
        self.assertEqual([row["candidates"] for row in result["rows"]],
                         [[str(sharp_s)], [str(double_s)]])
        filtered = core.scan(str(self.source), "first.ẞ\nsecond.ss",
                             include_extensions="ẞ", exclude_extensions="ss")
        self.assertEqual([row["status"] for row in filtered["rows"]], ["unique", "missing"])
        sensitive = core.scan(str(self.source), "ẞ", mode="extension", case_sensitive=True)
        self.assertEqual(sensitive["rows"][0]["status"], "missing")

    def test_invalid_extension_rule_is_visible_and_scan_is_incomplete(self):
        self.make_file("a.pdf")
        result = core.scan(str(self.source), "pdf\n*.tar.gz\n*\n../txt", mode="extension")
        self.assertFalse(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["uncertain", "invalid", "invalid", "invalid"])
        self.assertEqual(len(result["warnings"]), 3)

    def test_global_extension_filters_exclude_first_and_record_scope(self):
        self.make_file("a.pdf")
        self.make_file("b.txt")
        retained = self.make_file("README")
        self.make_file("image.png")
        result = core.scan(str(self.source), ".*", mode="regex",
                           include_extensions="pdf，txt；[无后缀]", exclude_extensions="*.pdf; .TXT")
        self.assertTrue(result["complete"])
        self.assertEqual(result["scanned"], 4)
        self.assertEqual(result["filtered_out"], 3)
        self.assertEqual(result["rows"][0]["candidates"], [str(retained)])
        exported = core.export_result(result, str(self.parent))
        summary = self.read_report(exported)[0]["匹配说明"]
        self.assertIn("包含后缀：[无后缀], .pdf, .txt", summary)
        self.assertIn("排除后缀：.pdf, .txt", summary)

    def test_filters_apply_to_exact_matching_and_support_sequences(self):
        self.make_file("one.PDF")
        self.make_file("two.txt")
        result = core.scan(str(self.source), "one.PDF\ntwo.txt", include_extensions=["PDF"])
        self.assertEqual([row["status"] for row in result["rows"]], ["unique", "missing"])
        case_sensitive = core.scan(str(self.source), "one.PDF", include_extensions=["pdf"], case_sensitive=True)
        self.assertEqual(case_sensitive["rows"][0]["status"], "missing")

    def test_invalid_filter_is_rejected_before_directory_access(self):
        for bad in ("pdf,*.tar.gz", ["pdf", ""], ["pdf", 7], "*/pdf"):
            with self.subTest(bad=bad), mock.patch.object(core, "_source_root") as source_root:
                with self.assertRaisesRegex(ValueError, "无效规则"):
                    core.scan(str(self.source), "one.pdf", include_extensions=bad)
                source_root.assert_not_called()

    def test_multi_selection_overlapping_rules_copy_each_path_once(self):
        first = self.make_file("a.pdf", b"a")
        second = self.make_file("b.pdf", b"b")
        third = self.make_file("c.txt", b"c")
        result = core.scan(str(self.source), r".*" + "\n" + r"\.pdf$", mode="regex")
        self.assertEqual([row["status"] for row in result["rows"]], ["multiple", "multiple"])
        for row in result["rows"]:
            row.update(status="selected", selected_candidates=list(row["candidates"]))
        exported = core.export_result(result, str(self.parent))
        output = Path(exported["output_dir"])
        self.assertEqual((exported["copied"], exported["failed"], exported["reused"]), (3, 0, 2))
        self.assertEqual((output / "a.pdf").read_bytes(), first.read_bytes())
        self.assertEqual((output / "b.pdf").read_bytes(), second.read_bytes())
        self.assertEqual((output / "c.txt").read_bytes(), third.read_bytes())
        rows = self.read_report(exported)
        self.assertEqual(set(rows[1]["实际选择路径"].splitlines()), {str(first), str(second), str(third)})
        self.assertEqual(rows[2]["导出状态"], "复用已复制文件")
        self.assertIn("复用已复制文件", rows[2]["复制明细"])
        self.assertIn("成功复制：3；复制失败：0", rows[0]["导出说明"])

    def test_multiple_requires_explicit_selection_and_only_copies_selected_subset(self):
        self.make_file("a.pdf")
        chosen = self.make_file("b.pdf", b"chosen")
        self.make_file("c.pdf")
        result = core.scan(str(self.source), "pdf", mode="extension")
        not_selected = core.export_result(result, str(self.parent))
        self.assertEqual(not_selected["copied"], 0)
        row = result["rows"][0]
        row.update(status="selected", selected_candidates=[str(chosen)])
        exported = core.export_result(result, str(self.parent))
        self.assertEqual(exported["copied"], 1)
        output = Path(exported["output_dir"])
        self.assertEqual([file.name for file in output.glob("*.pdf")], ["b.pdf"])
        self.assertEqual(len(self.read_report(exported)[1]["候选文件路径"].splitlines()), 3)

    def test_multi_selection_rejects_the_whole_row_if_any_member_is_invalid(self):
        chosen = self.make_file("a.pdf")
        self.make_file("b.pdf")
        outside = self.base / "outside.pdf"
        outside.write_bytes(b"private")
        result = core.scan(str(self.source), "pdf", mode="extension")
        result["rows"][0].update(status="selected", selected_candidates=[str(chosen), str(outside)])
        with mock.patch.object(core, "_copy_exclusive") as copy:
            exported = core.export_result(result, str(self.parent))
        copy.assert_not_called()
        self.assertEqual((exported["copied"], exported["failed"]), (0, 1))

    def test_multi_selection_reports_missing_file_but_copies_remaining_files(self):
        first = self.make_file("a.pdf", b"a")
        missing = self.make_file("b.pdf", b"b")
        last = self.make_file("c.pdf", b"c")
        result = core.scan(str(self.source), "pdf", mode="extension")
        result["rows"][0].update(status="selected", selected_candidates=[str(first), str(missing), str(last)])
        missing.unlink()
        exported = core.export_result(result, str(self.parent))
        self.assertEqual((exported["copied"], exported["failed"]), (2, 1))
        report = self.read_report(exported)[1]
        self.assertEqual(report["导出状态"], "部分完成")
        self.assertIn("复制失败", report["复制明细"])
        self.assertEqual(len(report["实际选择路径"].splitlines()), 3)

    def test_failed_shared_source_is_not_retried_or_counted_twice(self):
        missing = self.make_file("a.pdf")
        self.make_file("b.pdf")
        result = core.scan(str(self.source), r"\.pdf$" + "\n" + r"^[ab]", mode="regex")
        for row in result["rows"]:
            row.update(status="selected", selected_candidates=[str(missing)])
        missing.unlink()
        with mock.patch.object(core, "_copy_exclusive", wraps=core._copy_exclusive) as copy:
            exported = core.export_result(result, str(self.parent))
        self.assertEqual(copy.call_count, 1)
        self.assertEqual((exported["copied"], exported["failed"]), (0, 1))
        self.assertIn("此前处理未成功", self.read_report(exported)[2]["复制明细"])

    def test_large_rules_expand_to_complete_excel_safe_candidate_details(self):
        files = [self.make_file(f"batch/=invoice-{number:04}.pdf", b"document")
                 for number in range(1000)]
        result = core.scan(str(self.source), "=invoice\n" + r"\.pdf$", mode="regex")
        for row, amount in zip(result["rows"], (800, 900)):
            row.update(status="selected", selected_candidates=[str(path) for path in files[:amount]])
        files[7].unlink()
        exported = core.export_result(result, str(self.parent))
        self.assertEqual((exported["copied"], exported["failed"], exported["reused"]), (899, 1, 799))
        rows = self.read_report(exported)
        self.assertEqual([row["记录类型"] for row in rows[:3]], ["汇总", "清单项", "清单项"])
        self.assertTrue(all(row["记录类型"] == "候选明细" for row in rows[3:]))
        self.assertEqual(len(rows), 2003)
        self.assertEqual(rows[1]["候选文件路径"], core._DETAIL_NOTICE)
        self.assertEqual(rows[1]["实际选择路径"], core._DETAIL_NOTICE)
        self.assertEqual(rows[1]["目标路径"], core._DETAIL_NOTICE)
        self.assertEqual(rows[1]["复制明细"], core._DETAIL_NOTICE)
        all_paths = {str(path) for path in files}
        details = {}
        for line in ("1", "2"):
            records = [row for row in rows[3:] if row["清单行号"] == line]
            self.assertEqual({row["候选文件路径"] for row in records}, all_paths)
            details[line] = {row["候选文件路径"]: row for row in records}
        self.assertEqual(details["1"][str(files[0])]["导出状态"], "复制成功")
        self.assertEqual(details["1"][str(files[7])]["导出状态"], "复制失败")
        self.assertTrue(details["1"][str(files[7])]["导出说明"])
        self.assertEqual(details["1"][str(files[850])]["匹配说明"], "未选择")
        self.assertEqual(details["1"][str(files[850])]["实际选择路径"], "")
        self.assertEqual(details["1"][str(files[850])]["导出状态"], "未复制")
        self.assertEqual(details["2"][str(files[0])]["导出状态"], "复用已复制文件")
        self.assertEqual(details["2"][str(files[850])]["导出状态"], "复制成功")
        self.assertIn("此前处理未成功", details["2"][str(files[7])]["导出说明"])
        self.assertEqual(details["1"][str(files[0])]["清单文件名"], "'=invoice")
        for row in rows:
            for value in row.values():
                self.assertLessEqual(len(value.encode("utf-16-le")) // 2, 32767)
                self.assertLessEqual(value.count("\n"), 253)
        self.assertEqual(len(list(Path(exported["output_dir"]).rglob("*.pdf"))), 899)

    def test_many_short_paths_expand_for_excel_line_feed_limit_in_report_only_mode(self):
        files = [self.make_file(f"{number:03}.pdf") for number in range(260)]
        result = core.scan(str(self.source), "pdf", mode="extension")
        row = result["rows"][0]
        self.assertLess(len("\n".join(row["candidates"])), core._CSV_AGGREGATE_LIMIT)
        row.update(status="selected", selected_candidates=[str(files[0])])
        exported = core.export_result(result, str(self.parent), copy_files=False)
        rows = self.read_report(exported)
        self.assertEqual(exported["copied"], 0)
        self.assertEqual(len(rows), 262)
        self.assertEqual(rows[1]["候选文件路径"], core._DETAIL_NOTICE)
        details = {item["候选文件路径"]: item for item in rows[2:]}
        self.assertEqual(details[str(files[0])]["匹配说明"], "已选择")
        self.assertEqual(details[str(files[0])]["导出状态"], "仅导出报告")
        self.assertEqual(details[str(files[1])]["匹配说明"], "未选择")
        self.assertEqual(details[str(files[1])]["导出状态"], "未复制")
        self.assertFalse(any(item["目标路径"] for item in rows[2:]))

    def test_large_report_numeric_rule_name_is_protected_in_candidate_details(self):
        files = [self.make_file(f"000123-{number:03}.pdf") for number in range(260)]
        result = core.scan(str(self.source), "000123", mode="regex")
        exported = core.export_result(result, str(self.parent), copy_files=False)
        rows = self.read_report(exported)[1:]
        self.assertEqual(len(rows), 261)
        self.assertTrue(all(row["清单文件名"] == "'000123" for row in rows))
        self.assertTrue(all(row["清单行号"] == "1" for row in rows))
        self.assertEqual({row["候选文件路径"] for row in rows[1:]}, {str(path) for path in files})
        self.assertEqual(result["rows"][0]["query"], "000123")


if __name__ == "__main__":
    unittest.main()
