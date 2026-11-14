"""Local project catalog and portable, validated JSON project files."""
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
import time
from datetime import datetime
from contextlib import contextmanager

from providers import AppError

FORMAT = "novel-workshop-project"
MAX_PROJECT_BYTES = 128 * 1024 * 1024


@contextmanager
def connection(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid text")
    return value


def integer(value):
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        raise ValueError("invalid integer")
    return value


def basis(value):
    if not isinstance(value, dict) or set(value) - {"rules", "previous", "card", "versions", "writing_style", "review_requirements"}:
        raise ValueError("invalid basis")
    text(value["rules"], 16000)
    text(value.get("writing_style", ""), 12000)
    text(value.get("review_requirements", ""), 12000)
    text(value["card"].get("pacing", ""), 6000)
    if value["previous"] is not None:
        integer(value["previous"])
    for key, limit in (("title", 200), ("goal", 12000), ("tags", 1000), ("refs", 200)):
        text(value["card"][key], limit)
    if set(value["card"]) - {"title", "goal", "tags", "refs", "pacing"} or not isinstance(value["versions"], dict):
        raise ValueError("invalid basis fields")
    for key, version in value["versions"].items():
        if not re.fullmatch(r"[1-9][0-9]{0,8}", key):
            raise ValueError("invalid reference")
        integer(version)


def validate_workspace(value, chapters, drafts):
    if value is None:
        return None
    if isinstance(value, dict):
        value.setdefault("pacing", "")
        value.setdefault("review_requirements", "")
        value.setdefault("commentator_persona", "")
        value.setdefault("comment_reply", "")
    if not isinstance(value, dict) or set(value) != {"commentator_persona", "comment_reply", "pacing", "review_requirements", "active", "version", "number", "title", "goal", "tags", "refs", "body", "notes", "instruction", "rules", "style", "override"}:
        raise ValueError("invalid workspace")
    if value["active"] is not None and value["active"] not in chapters:
        raise ValueError("unknown workspace chapter")
    if value["version"] is not None and (value["version"] not in drafts or drafts[value["version"]]["chapter"] != value["active"]):
        raise ValueError("unknown workspace draft")
    # Keep an unfinished chapter number as typed, even if it is not yet a valid number.
    for key, limit in (("number", 20), ("title", 200), ("goal", 12000), ("tags", 1000), ("refs", 200),
                       ("body", 80000), ("notes", 16000), ("instruction", 14000), ("rules", 16000), ("style", 12000)):
        text(value[key], limit)
    text(value["commentator_persona"], 12000)
    text(value["comment_reply"], 12000)
    text(value["pacing"], 6000)
    text(value["review_requirements"], 12000)
    if type(value["override"]) is not bool:
        raise ValueError("invalid override")
    return value


def validate_project(value):
    try:
        if not isinstance(value, dict) or value.get("format") != FORMAT or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("unsupported format")
        if set(value) - {"format", "version", "name", "project_id", "rules", "style", "chapters", "drafts", "workspace", "review_requirements", "commentary", "auto_review", "commentator_persona"}:
            raise ValueError("unexpected fields")
        name = text(value["name"], 120).strip()
        if not name:
            raise ValueError("empty name")
        text(value["rules"], 16000)
        text(value["style"], 12000)
        text(value.get("review_requirements", ""), 12000)
        text(value.get("commentator_persona", ""), 12000)
        if type(value.get("auto_review", True)) is not bool:
            raise ValueError("invalid auto review")
        comment = value.get("commentary")
        if comment is not None:
            if not isinstance(comment, dict) or set(comment) - {"body", "signature", "model", "persona", "messages"}:
                raise ValueError("invalid commentary")
            text(comment["body"], 80000)
            text(comment["signature"], 64)
            text(comment["model"], 240)
            text(comment.get("persona", ""), 12000)
            messages = comment.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 200 or len(messages) % 2:
                raise ValueError("invalid discussion")
            for index, message in enumerate(messages):
                if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] != ("user" if index % 2 == 0 else "assistant"):
                    raise ValueError("invalid discussion role")
                text(message["content"], 12000 if message["role"] == "user" else 80000)
        if not isinstance(value["chapters"], list) or len(value["chapters"]) > 10000:
            raise ValueError("too many chapters")
        if not isinstance(value["drafts"], list) or len(value["drafts"]) > 200000:
            raise ValueError("too many drafts")
        chapters, drafts = {}, {}
        for card in value["chapters"]:
            if not isinstance(card, dict):
                raise ValueError("invalid chapter")
            card.setdefault("pacing", "")
            text(card["pacing"], 6000)
            if set(card) != {"number", "title", "goal", "tags", "refs", "canon", "pacing"}:
                raise ValueError("invalid chapter")
            number = integer(card["number"])
            if number in chapters:
                raise ValueError("duplicate chapter")
            for key, limit in (("title", 200), ("goal", 12000), ("tags", 1000), ("refs", 200)):
                text(card[key], limit)
            refs = [part for part in re.split(r"[,，、\s]+", card["refs"].strip()) if part]
            if len(refs) > 12 or any(not re.fullmatch(r"[1-9][0-9]{0,8}", r) or int(r) == number for r in refs):
                raise ValueError("invalid references")
            chapters[number] = card
        for draft in value["drafts"]:
            if set(draft) != {"id", "chapter", "body", "notes", "report", "writing_basis", "approved", "created"}:
                raise ValueError("invalid draft")
            identifier = integer(draft["id"])
            if identifier in drafts or integer(draft["chapter"]) not in chapters:
                raise ValueError("invalid draft reference")
            text(draft["body"], 80000)
            text(draft["notes"], 16000)
            text(draft["created"], 80)
            if type(draft["approved"]) is not int or draft["approved"] not in (0, 1):
                raise ValueError("invalid approval")
            if draft["writing_basis"] is not None:
                basis(draft["writing_basis"])
            report = draft["report"]
            if report is not None:
                if not isinstance(report, dict) or set(report) - {"issues", "notes", "basis", "mode", "provider", "model", "logic", "ai_style", "pacing", "draft_characters", "draft_sha256"}:
                    raise ValueError("invalid report")
                for key in ("logic", "ai_style", "pacing"):
                    text(report.get(key, ""), 16000)
                text(report.get("draft_characters", ""), 20)
                text(report.get("draft_sha256", ""), 64)
                basis(report["basis"])
                text(report["notes"], 16000)
                if report["mode"] not in ("demo", "api"):
                    raise ValueError("invalid report mode")
                for key in ("provider", "model"):
                    text(report.get(key, ""), 240)
                if not isinstance(report["issues"], list) or len(report["issues"]) > 10000:
                    raise ValueError("invalid issues")
                fields = {"kind", "severity", "draft_quote", "source_id", "source_quote", "reason", "suggestion", "draft_verified", "source_verified"}
                for issue in report["issues"]:
                    if set(issue) != fields:
                        raise ValueError("invalid issue fields")
                    for key in fields - {"draft_verified", "source_verified"}:
                        text(issue[key], 80000)
                    if any(type(issue[key]) is not bool for key in ("draft_verified", "source_verified")):
                        raise ValueError("invalid verification")
            drafts[identifier] = draft
        for card in chapters.values():
            canon = card["canon"]
            if canon is not None:
                integer(canon)
                if canon not in drafts or drafts[canon]["chapter"] != card["number"] or not drafts[canon]["approved"]:
                    raise ValueError("invalid canon")
        validate_workspace(value.get("workspace"), chapters, drafts)
        return value
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise AppError("项目文件格式无效、版本不支持或内容损坏；现有项目没有被修改。") from None


class ProjectStore:
    def __init__(self, directory, initialize):
        self.directory = directory
        self.initialize = initialize
        self.catalog = directory / "projects.sqlite3"
        with connection(self.catalog) as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS saved_files (path TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL, stamp TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY)")
            if not db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
                db.execute("INSERT INTO projects(id,name,filename) VALUES ('main','我的小说','novel.sqlite3')")
                db.execute("INSERT INTO metadata VALUES ('initialized')")
            db.execute("UPDATE projects SET deleted=1 WHERE id='demo'")

    def entries(self):
        with connection(self.catalog) as db:
            return [{"id": row[0], "name": row[1], "demo": False}
                    for row in db.execute("SELECT id,name FROM projects WHERE deleted=0 ORDER BY rowid")]

    def paths(self):
        with connection(self.catalog) as db:
            return {row[0]: self.directory / row[1] for row in db.execute("SELECT id,filename FROM projects WHERE deleted=0")}

    def create(self, name, content=None):
        name = self.valid_name(name)
        if content is not None:
            validate_project(content)
        identifier = uuid.uuid4().hex
        path = self.directory / ("project-" + identifier + ".sqlite3")
        try:
            self.initialize(path)
            if content is not None:
                with connection(path) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    db.executemany("INSERT INTO chapters(number,title,goal,tags,refs,canon,pacing) VALUES (:number,:title,:goal,:tags,:refs,:canon,:pacing)", content["chapters"])
                    for original in content["drafts"]:
                        draft = dict(original)
                        for key in ("report", "writing_basis"):
                            if draft[key] is not None:
                                draft[key] = json.dumps(draft[key], ensure_ascii=False)
                        db.execute("INSERT INTO drafts(id,chapter,body,notes,report,writing_basis,approved,created) VALUES (:id,:chapter,:body,:notes,:report,:writing_basis,:approved,:created)", draft)
                    db.executemany("INSERT INTO settings VALUES (?,?)", [("rules", content["rules"]), ("style", content["style"]), ("review_requirements", content.get("review_requirements", "")),
                        ("commentary", json.dumps(content.get("commentary"), ensure_ascii=False)),
                        ("auto_review", json.dumps(content.get("auto_review", True))),
                        ("commentator_persona", content.get("commentator_persona", "")),
                        ("workspace", json.dumps(content.get("workspace"), ensure_ascii=False))])
            with connection(self.catalog) as db:
                db.execute("INSERT INTO projects(id,name,filename) VALUES (?,?,?)", (identifier, name, path.name))
        except Exception:
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            raise
        return identifier

    def relocate(self, identifier, directory):
        destination = Path(directory).expanduser()
        if not destination.is_absolute():
            raise AppError("请输入绝对路径，例如 D:\\小说项目。")
        source = self.paths()[identifier]
        try:
            destination.mkdir(parents=True, exist_ok=True)
            destination = destination.resolve()
            target = destination / source.name
            if target.resolve() == source.resolve():
                return
            # Never overwrite an existing database, including another project's data.
            with target.open("xb"):
                pass
        except FileExistsError:
            raise AppError("目标目录已经有同名项目数据库，请选择其他目录，避免覆盖。") from None
        except OSError:
            raise AppError("保存目录不可用，请检查路径和写入权限。") from None
        try:
            with connection(source) as old, connection(target) as new:
                old.backup(new)
            with connection(self.catalog) as catalog:
                catalog.execute("UPDATE projects SET filename=? WHERE id=?", (str(target), identifier))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        # The former database remains a recovery copy; future writes use target.

    def delete(self, identifier):
        # Retain the local database as a recovery copy; it is no longer an active project.
        with connection(self.catalog) as db:
            if db.execute("UPDATE projects SET deleted=1 WHERE id=? AND deleted=0", (identifier,)).rowcount != 1:
                raise AppError("项目不存在或已经删除。")

    def name(self, identifier):
        return next(item["name"] for item in self.entries() if item["id"] == identifier)

    def valid_name(self, name, identifier=None):
        try:
            name = text(name, 120).strip()
            if (not name or name.endswith((".", " ")) or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                    or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name)):
                raise ValueError()
        except (TypeError, ValueError):
            raise AppError("项目名需为 1—120 个字符，不能包含 Windows 文件名禁用字符或保留名称。") from None
        if any(e["id"] != identifier and e["name"].casefold() == name.casefold() for e in self.entries()):
            raise AppError("已有同名项目，请使用其他名称。")
        return name

    def rename(self, identifier, name):
        name = self.valid_name(name, identifier)
        # Registered portable snapshots move with the project name. Check every
        # destination first; never overwrite files not owned by this project.
        moves = []
        with connection(self.catalog) as db:
            rows = db.execute("SELECT path,kind,stamp FROM saved_files WHERE project=?", (identifier,)).fetchall()
            for old, kind, stamp in rows:
                old = Path(old)
                if not old.exists():
                    continue
                suffix = "_" + stamp + "_自动保存" if kind == "auto" else ""
                new = old.with_name(name + suffix + ".novelproj")
                if old == new:
                    continue
                if new.exists() and new.resolve() != old.resolve():
                    raise AppError("新名称对应的项目文件已存在，请换一个名称，避免覆盖。")
                if not self.owned(old, identifier):
                    raise AppError("已有保存文件被替换，请先移走该文件再重命名。")
                moves.append((old, new))
            completed = []
            try:
                for old, new in moves:
                    os.rename(old, new)
                    completed.append((old, new))
                    db.execute("UPDATE saved_files SET path=? WHERE path=?", (str(new), str(old)))
                db.execute("UPDATE projects SET name=? WHERE id=?", (name, identifier))
            except Exception:
                for old, new in reversed(completed):
                    os.rename(new, old)
                raise
        # Stored content keeps the original historical name until the next save;
        # import uses the snapshot's recorded name, never changes its text.

    @staticmethod
    def owned(path, identifier):
        try:
            if path.is_symlink() or path.stat().st_size > MAX_PROJECT_BYTES:
                return False
            content = json.loads(path.read_text(encoding="utf-8"))
            return content.get("format") == FORMAT and content.get("project_id") == identifier
        except (OSError, ValueError, AttributeError):
            return False

    def save(self, identifier, content, automatic=False):
        name = self.valid_name(self.name(identifier), identifier)
        directory = self.paths()[identifier].parent.resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") if automatic else ""
        suffix = "_" + stamp + "_自动保存" if automatic else ""
        target = directory / (name + suffix + ".novelproj")
        temporary = directory / (".save-" + uuid.uuid4().hex + ".tmp")
        try:
            with connection(self.catalog) as db:
                registered = db.execute("SELECT project FROM saved_files WHERE path=?", (str(target),)).fetchone()
                if target.exists() and (not registered or registered[0] != identifier or not self.owned(target, identifier)):
                    raise AppError("保存目录里已有同名文件且不属于本项目，请更换目录或项目名称。")
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(content, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                if automatic:
                    # Prune only registered, still-owned snapshots, after the
                    # new snapshot has been fully written and fsynced. Keep the
                    # newest old snapshot until the atomic install succeeds.
                    old = db.execute("SELECT path FROM saved_files WHERE project=? AND kind='auto' ORDER BY created DESC, stamp DESC", (identifier,)).fetchall()[1:]
                    for (filename,) in old:
                        path = Path(filename)
                        if self.owned(path, identifier):
                            path.unlink()
                        db.execute("DELETE FROM saved_files WHERE path=?", (filename,))
                os.replace(temporary, target)
                db.execute("INSERT OR REPLACE INTO saved_files VALUES (?,?,?,?,?)", (str(target), identifier, "auto" if automatic else "manual", time.time(), stamp))
        except OSError:
            raise AppError("项目保存或自动保存清理失败，请检查目录权限和剩余空间。") from None
        finally:
            temporary.unlink(missing_ok=True)
        return target
