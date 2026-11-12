"""Offline, copy-only selection; user regexes use a bounded regex engine."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence

try:
    import regex as regex_engine
except ImportError:
    regex_engine = None


_INVALID = set('<>:"/\\|?*')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_CHUNK_SIZE = 1024 * 1024
_REGEX_TIMEOUT = 0.05
_CSV_AGGREGATE_LIMIT = 24000
_CSV_LINE_BREAK_LIMIT = 240
_DETAIL_NOTICE = "路径较多，见本表后续候选明细记录。"
_MATCH_LABELS = {
    "unique": "唯一匹配", "missing": "未找到", "ambiguous": "多份匹配",
    "duplicate": "清单重复", "invalid": "输入无效", "uncertain": "尚未确定",
    "selected": "人工选择",
    "multiple": "匹配多个文件",
}
_EXPORT_LABELS = {
    "not_selected": "未复制", "report_only": "仅导出报告", "cancelled": "已取消",
    "failed": "复制失败", "copied": "复制成功",
    "reused": "复用已复制文件", "partial": "部分完成",
}
_MODE_LABELS = {"full": "完整文件名", "stem": "不含扩展名",
                "regex": "正则表达式", "extension": "文件后缀"}


class _Cancelled(Exception):
    pass


def _cancelled(event):
    return event is not None and event.is_set()


def _notify(callback, message):
    if callback is not None:
        try:
            callback(message)
        except Exception:
            # A closed UI must not turn a completed copy into a failed copy.
            pass


def _is_reparse(path, info=None):
    info = info if info is not None else os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _inside(path, root):
    try:
        return os.path.normcase(os.path.commonpath([str(path), str(root)])) == os.path.normcase(str(root))
    except ValueError:
        return False


def _source_root(source):
    raw = Path(os.path.abspath(os.fspath(source)))
    if _is_reparse(raw):
        raise ValueError("来源目录不能是符号链接或 Windows 重解析目录。")
    root = raw.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("来源必须是现有文件夹。")
    return root


def _name_error(query):
    if any(ord(char) < 32 or char in _INVALID for char in query):
        return "只接受单列文件名，不接受路径、通配符或控制字符。"
    if query in (".", "..") or query.endswith((".", " ")):
        return "文件名不能是点号，也不能以点号或空格结尾。"
    if _RESERVED.match(query):
        return "这是 Windows 保留设备名，不能作为普通文件名。"
    try:
        if len(query.encode("utf-16-le")) // 2 > 255:
            return "文件名超过 Windows 单个名称的长度限制。"
    except UnicodeEncodeError:
        return "文件名含有无法编码的 Unicode 字符。"
    return ""


def _name_key(value):
    """Ignore one-character case differences without expanding names.

    This deliberately does not reproduce every Windows Unicode equivalence.
    In particular, sharp s may match capital sharp s, but never two s letters.
    """
    if value.isascii():
        return value.lower()
    normalized = []
    for char in value:
        folded = char.casefold()
        if len(folded) == 1:
            normalized.append(folded)
        else:
            lowered = char.lower()
            normalized.append(lowered if len(lowered) == 1 else char)
    return "".join(normalized)


def _normalize_extension(value, case_sensitive=False):
    if not isinstance(value, str):
        raise ValueError("后缀规则必须是文本。")
    value = value.strip()
    if value == "[无后缀]":
        return ""
    if value.startswith("*."):
        value = value[2:]
    elif value.startswith("."):
        value = value[1:]
    if not value or any(char.isspace() or ord(char) < 32 or char in _INVALID
                        or char in ".,;，；" for char in value):
        raise ValueError("后缀只接受 pdf、.pdf、*.pdf 这类单个最终后缀，或 [无后缀]。")
    return "." + (value if case_sensitive else _name_key(value))


def _extension_filter(value, label, case_sensitive):
    if isinstance(value, str):
        tokens = [token for token in re.split(r"[\s,，;；]+", value) if token]
    elif isinstance(value, Sequence):
        tokens = list(value)
    else:
        raise ValueError(f"{label}必须是分隔后的文本或后缀列表。")
    normalized = set()
    for token in tokens:
        try:
            normalized.add(_normalize_extension(token, case_sensitive))
        except ValueError as exc:
            raise ValueError(f"{label}含无效规则 {token!r}：{exc}") from exc
    return normalized


def _show_extensions(values):
    return [value or "[无后缀]" for value in sorted(values)]


def scan(source: str, text: str, mode="full", recursive=True,
         case_sensitive=False, cancel_event=None, progress=None,
         include_extensions="", exclude_extensions="") -> dict:
    """Read a directory tree; never modify source files.

    Nonblank input lines are preserved literally, including leading spaces and
    zeroes. In stem mode only the source file's final suffix is removed.
    Links/reparse entries are excluded from scope and explicitly reported.
    Any traversal failure or cancellation makes the scan incomplete. Regexes
    search complete file names with a timeout on each matching operation.
    Extension rules use only the final suffix; exclusions override inclusions.
    """
    if mode not in _MODE_LABELS:
        raise ValueError("mode 必须是 full、stem、regex 或 extension。")
    if not isinstance(text, str):
        raise TypeError("清单必须是文本。")
    included = _extension_filter(include_extensions, "包含后缀", case_sensitive)
    excluded = _extension_filter(exclude_extensions, "排除后缀", case_sensitive)
    if mode == "regex" and regex_engine is None:
        raise RuntimeError("缺少支持超时保护的正则组件；请使用完整安装包或安装 regex。")
    root = _source_root(source)
    key = (lambda value: value) if case_sensitive else _name_key
    rows, first_seen, wanted, row_keys, patterns = [], {}, {}, {}, {}
    warnings, complete = [], True
    # split on actual line terminators, not all Unicode separators in names.
    for line_number, raw in enumerate(re.split(r"\r\n|\n|\r", text), 1):
        if not raw.strip():
            continue
        query = raw
        row = {"line": line_number, "query": query, "status": "missing",
               "candidates": [], "reason": ""}
        error, compiled = "", None
        query_key = key(query)
        if mode == "regex":
            # Regex syntax is case-sensitive even if its matching is not:
            # e.g. \d and \D must never be collapsed into one rule.
            query_key = query
            try:
                compiled = regex_engine.compile(query, regex_engine.VERSION1 |
                                                (0 if case_sensitive else regex_engine.IGNORECASE))
            except (regex_engine.error, ValueError, OverflowError, RecursionError) as exc:
                error = f"正则表达式无效：{exc}"
        elif mode == "extension":
            try:
                query_key = _normalize_extension(query, case_sensitive)
            except ValueError as exc:
                error = str(exc)
        else:
            error = _name_error(query)
        if error:
            row.update(status="invalid", reason=error)
            if mode in ("regex", "extension"):
                complete = False
                warnings.append(f"第 {line_number} 行规则无效，本次扫描不完整：{error}")
        elif query_key in first_seen:
            row.update(status="duplicate", reason=f"与第 {first_seen[query_key]} 行重复，不重复复制。")
        else:
            first_seen[query_key] = line_number
            wanted[query_key] = []
            row_keys[line_number] = query_key
            if compiled is not None:
                patterns[query_key] = (compiled, row)
        rows.append(row)

    scanned, filtered_out = 0, 0
    pending = [root]
    while pending:
        if _cancelled(cancel_event):
            complete = False
            warnings.append("用户取消扫描；结果不是完整目录清单。")
            break
        directory = pending.pop()
        try:
            # Recheck queued paths because a directory may have changed since
            # its parent was enumerated.
            if _is_reparse(directory) or not _inside(directory.resolve(strict=True), root):
                complete = False
                warnings.append(f"目录在扫描期间发生变化，未继续读取：{directory}")
                continue
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError as exc:
            complete = False
            warnings.append(f"无法读取目录 {directory}：{exc}")
            continue
        for entry in entries:
            if _cancelled(cancel_event):
                complete = False
                warnings.append("用户取消扫描；结果不是完整目录清单。")
                pending.clear()
                break
            try:
                info = entry.stat(follow_symlinks=False)
                if _is_reparse(entry.path, info):
                    warnings.append(f"已跳过符号链接或重解析项：{entry.path}")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if recursive:
                        pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    warnings.append(f"已跳过非普通文件：{entry.path}")
                    continue
                scanned += 1
                if scanned % 500 == 0:
                    _notify(progress, f"已扫描 {scanned} 个文件")
                suffix = key(Path(entry.name).suffix)
                if suffix in excluded or (included and suffix not in included):
                    filtered_out += 1
                    continue
                if mode == "regex":
                    for pattern_key, (compiled, pattern_row) in list(patterns.items()):
                        if _cancelled(cancel_event):
                            break
                        try:
                            if compiled.search(entry.name, timeout=_REGEX_TIMEOUT) is not None:
                                wanted[pattern_key].append(str(Path(entry.path)))
                        except TimeoutError:
                            complete = False
                            reason = f"正则匹配超过 {_REGEX_TIMEOUT:g} 秒，已停止使用此规则。"
                            pattern_row.update(status="invalid", reason=reason)
                            warnings.append(f"第 {pattern_row['line']} 行 {reason}本次扫描不完整。")
                            del patterns[pattern_key]
                        except (regex_engine.error, ValueError, OverflowError, RecursionError) as exc:
                            complete = False
                            reason = f"正则匹配失败，已停止使用此规则：{exc}"
                            pattern_row.update(status="invalid", reason=reason)
                            warnings.append(f"第 {pattern_row['line']} 行 {reason}；本次扫描不完整。")
                            del patterns[pattern_key]
                else:
                    if mode == "extension":
                        lookup = suffix
                    else:
                        name = entry.name if mode == "full" else Path(entry.name).stem
                        lookup = key(name)
                    if lookup in wanted:
                        wanted[lookup].append(str(Path(entry.path)))
            except OSError as exc:
                complete = False
                warnings.append(f"无法检查 {entry.path}：{exc}")
    if _cancelled(cancel_event):
        complete = False
        if not any("用户取消扫描" in warning for warning in warnings):
            warnings.append("用户取消扫描；结果不是完整目录清单。")
    for row in rows:
        if row["status"] in ("invalid", "duplicate"):
            if row["line"] in row_keys:
                row["candidates"] = sorted(wanted[row_keys[row["line"]]])
            continue
        candidates = sorted(wanted[row_keys[row["line"]]], key=lambda value: (value.casefold(), value))
        row["candidates"] = candidates
        if not complete and mode in ("regex", "extension"):
            row.update(status="uncertain", reason="扫描未完成，匹配文件列表可能不全。")
        elif len(candidates) > 1 and mode in ("regex", "extension"):
            row.update(status="multiple", reason=f"匹配 {len(candidates)} 个文件，请明确选择需要复制的文件。")
        elif len(candidates) > 1:
            row.update(status="ambiguous", reason=f"找到 {len(candidates)} 份同名文件，请人工核对。")
        elif not complete:
            row.update(status="uncertain", reason="扫描未完成，无法确认是否缺失或唯一。")
        elif candidates:
            row.update(status="unique", reason="唯一匹配。")
        else:
            row.update(status="missing", reason="在本次扫描和后缀筛选范围内未找到。")
    _notify(progress, f"扫描{'完成' if complete else '未完成'}：{scanned} 个文件")
    return {"source": str(root), "mode": mode, "scanned": scanned,
            "rows": rows, "warnings": warnings, "complete": complete,
            "recursive": bool(recursive), "case_sensitive": bool(case_sensitive),
            "include_extensions": _show_extensions(included),
            "exclude_extensions": _show_extensions(excluded), "filtered_out": filtered_out}


def _safe_source_file(candidate, root):
    """Validate the lexical path and every component without trusting a scan."""
    path = Path(os.path.abspath(os.fspath(candidate)))
    if not _inside(path, root) or path == root:
        raise ValueError("候选文件不在来源目录内。")
    if _is_reparse(root):
        raise ValueError("来源目录已变成符号链接或重解析目录。")
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if _is_reparse(current):
            raise ValueError("候选路径包含符号链接或 Windows 重解析项。")
    if not _inside(path.resolve(strict=True), root):
        raise ValueError("候选文件解析后位于来源目录之外。")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("候选对象不再是普通文件。")
    return path, relative, info


def _destination_path(output, relative):
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("输出相对路径无效。")
    current = output
    if _is_reparse(output):
        raise ValueError("输出目录已变成链接。")
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            if _is_reparse(current) or not current.is_dir():
                raise ValueError("输出路径被其他文件或链接占用。")
        if not _inside(current.resolve(strict=True), output):
            raise ValueError("输出路径越过本次导出目录。")
    return current / relative.name


def _same_identity(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _copy_exclusive(candidate, root, output, cancel_event):
    if _cancelled(cancel_event):
        raise _Cancelled("复制已取消。")
    path, relative, before = _safe_source_file(candidate, root)
    destination = _destination_path(output, relative)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = None
            opened = os.fstat(source_file.fileno())
            if not _same_identity(before, opened) or not stat.S_ISREG(opened.st_mode):
                raise ValueError("来源文件在打开时发生变化，未复制。")
            _safe_source_file(candidate, root)
            if _cancelled(cancel_event):
                raise _Cancelled("复制已取消。")
            # xb guarantees an existing target is never overwritten.
            with open(destination, "xb") as target_file:
                while True:
                    if _cancelled(cancel_event):
                        raise _Cancelled("复制中途取消，目标中可能保留不完整文件。")
                    chunk = source_file.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    target_file.write(chunk)
                after = os.fstat(source_file.fileno())
                if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError("来源文件在复制期间发生变化；已保留输出，请重新核对。")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return str(destination)


def _csv_safe(value):
    value = "" if value is None else str(value)
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_list_name(value):
    # CSV has no text-column type. A reversible prefix prevents spreadsheet
    # import from dropping leading zeroes or rounding long numeric names.
    # Only the list-name column uses this; line numbers remain ordinary values.
    if value.isascii() and value.isdecimal():
        return "'" + value
    return value


def _row_selection(row):
    """Return explicit selections plus a validation error; never guess."""
    candidates = row.get("candidates", [])
    if row["status"] == "unique":
        if not isinstance(candidates, list) or len(candidates) != 1:
            return [], "唯一匹配行的候选数量异常，未复制。"
        if not isinstance(candidates[0], str) or not os.path.isabs(candidates[0]):
            return [], "唯一匹配行缺少有效的候选绝对路径，未复制。"
        return [candidates[0]], ""
    if row["status"] != "selected":
        return [], ""
    if "selected_candidates" in row:
        raw = row["selected_candidates"]
        if not isinstance(raw, list) or not raw:
            return [], "人工选择缺少有效的候选绝对路径列表，未复制。"
        requested = list(raw)
    else:
        raw = row.get("selected_candidate")
        requested = [raw] if isinstance(raw, str) else []
    if not isinstance(candidates, list) or len(candidates) < 2:
        return requested, "人工选择要求原候选列表至少包含两份文件，未复制。"
    if not requested or any(not isinstance(item, str) or not item or not os.path.isabs(item)
                            for item in requested):
        return requested, "人工选择缺少有效的候选绝对路径，未复制。"
    if any(item not in candidates for item in requested):
        return requested, "人工选择不属于原候选列表，未复制。"
    return list(dict.fromkeys(requested)), ""


def _planned_destination(candidate, root, output):
    try:
        relative = Path(os.path.abspath(candidate)).relative_to(root)
        return str(output / relative)
    except (ValueError, TypeError):
        return ""


def _needs_candidate_details(fields):
    # Excel supports 32,767 characters and 253 line feeds per cell. Leave
    # margin and count UTF-16 units conservatively, including non-BMP names.
    return any(len(value.encode("utf-16-le")) // 2 > _CSV_AGGREGATE_LIMIT
               or value.count("\n") > _CSV_LINE_BREAK_LIMIT for value in fields)


def _candidate_detail_rows(row, candidates, selections, outcomes,
                           copy_files, selection_error):
    """Expand a large aggregate without losing unselected or failed paths."""
    chosen = {str(item) for item in selections}
    results = {str(item["source"]): item for item in outcomes}
    original_candidates = [str(item) for item in candidates]
    candidate_set = set(original_candidates)
    # Malformed selections still need a trace if their aggregate is expanded.
    extra_selections = [item for item in chosen if item not in candidate_set]
    records = []
    for candidate in original_candidates + sorted(extra_selections):
        is_selected = candidate in chosen
        outcome = results.get(candidate)
        target = ""
        if outcome is not None:
            status, target, explanation = outcome["status"], outcome["destination"], outcome["detail"]
        elif is_selected and selection_error:
            status, explanation = "failed", selection_error
        elif is_selected and not copy_files:
            status, explanation = "report_only", "本次只导出报告，没有复制此文件。"
        else:
            status, explanation = "not_selected", "此候选未在本行选择，未通过本行复制。"
        records.append([
            "候选明细" if candidate in candidate_set else "选择异常",
            row["line"], _csv_list_name(row["query"]), _MATCH_LABELS.get(row["status"], "未知状态"),
            candidate, "已选择" if is_selected else "未选择", _EXPORT_LABELS[status],
            target, explanation, candidate if is_selected else "", explanation,
        ])
    return records


def export_result(result, parent: str, copy_files=True, cancel_event=None,
                  progress=None) -> dict:
    """Create a unique output directory and an explicit per-input CSV report.

    Any partial files remain in that directory and are marked failed/cancelled
    in the report. Source files are never moved, removed or overwritten.
    Selected rows must name exact members of their original candidate list.
    A source path shared by several rows is copied at most once. There is
    never a fallback to an unselected candidate after a failure.
    """
    if copy_files and not result.get("complete", False):
        raise ValueError("扫描未完成，禁止复制；可以仅导出核对报告。")
    # Reports can be exported even if the original root no longer exists.
    root = Path(os.path.abspath(os.fspath(result["source"])))
    raw_parent = Path(os.path.abspath(os.fspath(parent)))
    output_parent = raw_parent.resolve(strict=True)
    if not output_parent.is_dir():
        raise ValueError("输出父目录必须是现有文件夹。")
    resolved_root = root.resolve(strict=False)
    if _inside(raw_parent, root) or _inside(output_parent, resolved_root):
        raise ValueError("输出父目录不能位于来源目录内部。")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    output = Path(tempfile.mkdtemp(prefix=f"清单取件-{stamp}-", dir=output_parent))
    report_path = output / "核对报告.csv"
    copied, failed, reused = 0, 0, 0
    cancelled = _cancelled(cancel_event)
    copy_cache = {}
    report_rows = []
    candidate_details = []
    for row in result["rows"]:
        destination, export_status, detail = "", "not_selected", "该行尚未确认可复制文件，未复制。"
        candidates = row.get("candidates", [])
        selections, selection_error = _row_selection(row)
        outcomes, copy_details = [], ""
        if selection_error:
            failed += 1
            export_status, detail = "failed", selection_error
        elif not copy_files:
            export_status, detail = "report_only", "本次只导出报告。"
        elif row["status"] in ("unique", "selected"):
            for candidate in selections:
                candidate_key = os.path.normcase(os.path.abspath(candidate))
                if candidate_key in copy_cache:
                    previous = copy_cache[candidate_key]
                    if previous["status"] == "copied":
                        reused += 1
                        outcome = {**previous, "status": "reused",
                                   "detail": "已由其他清单项复制，本次未重复复制。"}
                    else:
                        outcome = {**previous, "detail": "此前处理未成功，本次未重复尝试：" + previous["detail"]}
                    outcomes.append({**outcome, "source": candidate})
                    continue
                target = _planned_destination(candidate, root, output)
                if cancelled or _cancelled(cancel_event):
                    cancelled = True
                    outcomes.append({"source": candidate, "destination": target,
                                     "status": "cancelled", "detail": "用户取消，未开始复制此项。"})
                    continue
                _notify(progress, f"正在复制：{candidate}")
                try:
                    target = _copy_exclusive(candidate, root, output, cancel_event)
                    copied += 1
                    outcome = {"status": "copied", "destination": target, "detail": "复制成功。"}
                except _Cancelled as exc:
                    cancelled = True
                    outcome = {"status": "cancelled", "destination": target, "detail": str(exc)}
                except (OSError, ValueError, TypeError) as exc:
                    failed += 1
                    outcome = {"status": "failed", "destination": target, "detail": str(exc)}
                copy_cache[candidate_key] = outcome
                outcomes.append({**outcome, "source": candidate})
            counts = {code: sum(item["status"] == code for item in outcomes)
                      for code in ("copied", "reused", "failed", "cancelled")}
            if counts["cancelled"]:
                export_status = "cancelled"
            elif counts["failed"]:
                export_status = "partial" if counts["copied"] + counts["reused"] else "failed"
            elif counts["copied"]:
                export_status = "copied"
            elif counts["reused"]:
                export_status = "reused"
            copy_details = "\n".join(
                f"{index}. {_EXPORT_LABELS[item['status']]}：{item['source']}"
                + (f" → {item['destination']}" if item["destination"] else "")
                + f"；{item['detail']}" for index, item in enumerate(outcomes, 1)
            )
            destination = "\n".join(item["destination"] for item in outcomes if item["destination"])
            detail = (f"已确认 {len(selections)} 份；本行新复制 {counts['copied']} 份，"
                      f"复用 {counts['reused']} 份，失败 {counts['failed']} 份，取消 {counts['cancelled']} 份。"
                      + ("\n" + copy_details if copy_details else ""))
        report_row = [
            "清单项", row["line"], _csv_list_name(row["query"]), _MATCH_LABELS.get(row["status"], "未知状态"),
            "\n".join(str(item) for item in candidates), row.get("reason", ""),
            _EXPORT_LABELS[export_status], destination, detail,
            "\n".join(str(item) for item in selections), copy_details,
        ]
        if _needs_candidate_details([report_row[index] for index in (4, 7, 8, 9, 10)]):
            candidate_details.extend(_candidate_detail_rows(
                row, candidates, selections, outcomes, copy_files, selection_error
            ))
            for index in (4, 7, 9, 10):
                if report_row[index]:
                    report_row[index] = _DETAIL_NOTICE
            # Preserve the per-rule counts while moving its per-file details.
            report_row[8] = detail.split("\n", 1)[0] + "\n" + _DETAIL_NOTICE
        report_rows.append(report_row)
    cancelled = cancelled or _cancelled(cancel_event)
    # Keep all original input rows together at the front for compatibility.
    report_rows.extend(candidate_details)
    report_rows.insert(0, ["汇总", "", "", "扫描完整" if result.get("complete") else "扫描未完成",
                           result["source"], f"扫描文件数：{result.get('scanned', 0)}；匹配模式：{_MODE_LABELS.get(result.get('mode'), '未知模式')}；"
                           f"包含后缀：{', '.join(result.get('include_extensions', [])) or '全部'}；"
                           f"排除后缀：{', '.join(result.get('exclude_extensions', [])) or '无'}；"
                           f"筛掉文件数：{result.get('filtered_out', 0)}",
                           "已取消" if cancelled else "处理结束", str(output),
                           f"成功复制：{copied}；复制失败：{failed}；复用已复制文件：{reused}；部分文件不会自动删除。", "", ""])
    for warning in result.get("warnings", []):
        report_rows.append(["警告", "", "", "", "", warning, "", "", "", "", ""])
    report_error = ""
    try:
        # A selected source file may itself be named 核对报告.csv. Keep both:
        # never overwrite a copied file merely to reserve the report's name.
        report_number = 1
        while True:
            try:
                report = open(report_path, "x", encoding="utf-8-sig", newline="")
                break
            except FileExistsError:
                report_number += 1
                report_path = output / f"核对报告-{report_number}.csv"
        with report:
            writer = csv.writer(report)
            writer.writerow(["记录类型", "清单行号", "清单文件名", "匹配状态", "候选文件路径",
                             "匹配说明", "导出状态", "目标路径", "导出说明", "实际选择路径", "复制明细"])
            writer.writerows([_csv_safe(value) for value in values] for values in report_rows)
    except (OSError, UnicodeError) as exc:
        report_error = f"核对报告写入失败：{exc}；已产生的输出保留在 {output}"
    _notify(progress, f"导出结束：成功 {copied}，失败 {failed}" + ("，已取消" if cancelled else ""))
    return {"output_dir": str(output), "copied": copied, "failed": failed,
            "cancelled": cancelled, "report_path": str(report_path) if not report_error else "",
            "report_error": report_error, "reused": reused}
