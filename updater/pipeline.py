"""What «Перевірити» and «Оновити» do.

The order is built around one line: everything before `switch` is
preparation and leaves the running application untouched, so a failure there
ends in «Не виконано» with nothing to undo. From `switch` on the application
is being replaced, and a failure is rolled back to the commits, images and —
if the migrations moved — the database it started from.
"""
import tempfile
from datetime import datetime
from pathlib import Path

from updater.ops import IMAGE_INPUTS, CommandError, Git, Platform
from updater.store import Job

KINDS = ("backend", "frontend")
LABELS = {"backend": "Бекенд", "frontend": "Фронтенд"}

UPDATED = "updated"
UP_TO_DATE = "up_to_date"
ROLLED_BACK = "rolled_back"
NOT_DONE = "not_done"
FAILED = "failed"

UPDATER_DIR = "updater/"


class Abort(Exception):
    """Stop before anything was changed."""


def check(repos: dict[str, Git], config: dict) -> dict:
    """Fetch both repositories and describe what an update would bring."""
    token = config.get("token")
    result = {"checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "repos": {}, "problems": [], "notes": []}
    for kind, git in repos.items():
        rc = config["repos"][kind]
        entry: dict = {"url": rc["url"], "branch": rc["branch"], "error": None}
        result["repos"][kind] = entry
        head = git.head()
        entry["head"] = git.info(head)
        try:
            remote = git.fetch(rc["url"], rc["branch"], token)
        except CommandError as exc:
            entry["error"] = f"Не вдалося отримати {rc['url']} ({rc['branch']}): {exc.output.strip()[-300:]}"
            result["problems"].append(entry["error"])
            continue
        entry["remote"] = git.info(remote)
        entry["dirty"] = git.dirty()
        if remote == head or git.is_ancestor(remote, head):
            entry["ahead"] = 0
            entry["commits"] = []
            if remote != head:
                result["notes"].append(f"{LABELS[kind]}: на сервері новіші коміти, ніж у {rc['branch']}")
            continue
        if not git.is_ancestor(head, remote):
            entry["diverged"] = True
            result["problems"].append(
                f"{LABELS[kind]}: на сервері є власні коміти, яких немає в {rc['branch']} — "
                "оновлення без переписування неможливе"
            )
        entry["ahead"] = git.count(head, remote)
        entry["commits"] = git.commits(head, remote)
        if entry["dirty"]:
            result["problems"].append(
                f"{LABELS[kind]}: на сервері змінено файли проєкту: {', '.join(entry['dirty'][:10])}"
            )
        collisions = git.untracked_collisions(head, remote)
        if collisions:
            result["problems"].append(
                f"{LABELS[kind]}: файли заважатимуть оновленню: {', '.join(collisions[:10])}"
            )
        if kind == "backend":
            changed = git.changed_files(head, remote)
            entry["migrations"] = git.migration_heads(head) != git.migration_heads(remote)
            entry["image_rebuild"] = any(p in IMAGE_INPUTS for p in changed)
            entry["updater_changed"] = any(p.startswith(UPDATER_DIR) for p in changed)
            if entry["migrations"]:
                result["notes"].append("Оновлення змінює структуру бази (міграції)")
            if entry["image_rebuild"]:
                result["notes"].append("Змінилися залежності — образ бекенда буде перезібрано (кілька хвилин)")
            if entry["updater_changed"]:
                result["notes"].append(
                    "Змінився код самого updater — після оновлення перезапустіть його: "
                    "docker compose up -d --build updater"
                )
    return result


class Update:
    """One run of the pipeline. Remembers every change it made, for rollback."""

    def __init__(self, job: Job, repos: dict[str, Git], platform: Platform, settings):
        self.job = job
        self.repos = repos
        self.p = platform
        self.s = settings
        self.changed: list[str] = []          # kinds whose commit moves
        self.old: dict[str, str] = {}
        self.new: dict[str, str] = {}
        self.built: set[str] = set()
        self.swapped: set[str] = set()
        self.merged: set[str] = set()
        self.scheduler_stopped = False
        self.old_revisions: set[str] = set()
        self.new_heads: set[str] = set()
        self.dump_path: Path | None = None
        self.image_rebuild = False

    # ── entry point ───────────────────────────────────────────────────────
    def run(self) -> None:
        log = self.job.log
        try:
            if not self._prepare():
                return
            self._build()
            self._backup()
        except Exception as exc:  # noqa: BLE001 — nothing is changed yet, whatever it was
            log(f"Зупинено до заміни: {exc}")
            self._undo_preparation()
            self.job.finish(NOT_DONE, f"Не виконано: {exc}")
            return

        try:
            self._switch()
            self._verify()
        except Exception as exc:  # noqa: BLE001 — anything here must roll back
            log(f"Помилка після початку заміни: {exc}")
            self._rollback(str(exc))
            return

        self.p.prune()
        self.job.finish(UPDATED, "Оновлено")

    # ── before the switch ─────────────────────────────────────────────────
    def _prepare(self) -> bool:
        job = self.job
        job.step("prepare", "running")
        target = job.data["target"]
        for kind in KINDS:
            git = self.repos[kind]
            head = git.head()
            self.old[kind] = head
            want = target.get(kind) or head
            if want == head:
                continue
            if not git.has_commit(want):
                raise Abort(f"{LABELS[kind]}: коміт {want[:8]} не знайдено — натисніть «Перевірити» ще раз")
            if not git.is_ancestor(head, want):
                raise Abort(f"{LABELS[kind]}: {want[:8]} не продовжує встановлену версію")
            dirty = git.dirty()
            if dirty:
                raise Abort(f"{LABELS[kind]}: на сервері змінено файли: {', '.join(dirty[:10])}")
            collisions = git.untracked_collisions(head, want)
            if collisions:
                raise Abort(f"{LABELS[kind]}: файли заважатимуть оновленню: {', '.join(collisions[:10])}")
            self.new[kind] = want
            self.changed.append(kind)
        job.set(**{"from": {k: v for k, v in self.old.items()}})

        if not self.changed:
            job.step("prepare", "done", "Нових змін немає")
            for step in ("build", "backup", "switch", "verify"):
                job.step(step, "skipped")
            job.finish(UP_TO_DATE, "Вже остання версія")
            return False

        if "backend" in self.changed:
            if self.p.data_update_running():
                raise Abort("іде оновлення даних (hostlib) — дочекайтеся його завершення")
            backend = self.repos["backend"]
            changed_files = backend.changed_files(self.old["backend"], self.new["backend"])
            self.image_rebuild = any(p in IMAGE_INPUTS for p in changed_files)
            if any(p.startswith(UPDATER_DIR) for p in changed_files):
                job.warn("Змінився код updater — перезапустіть його: docker compose up -d --build updater")
            self.old_revisions = self.p.db_revisions()
            self.new_heads = backend.migration_heads(self.new["backend"])
            size = self.p.db_size()
            free = self.p.free_space()
            # A custom-format dump compresses well; the full size is the margin.
            if free < size:
                raise Abort(f"мало місця для копії бази: вільно {free >> 20} МБ, база {size >> 20} МБ")
        notes = ", ".join(f"{LABELS[k]} {self.old[k][:8]} → {self.new[k][:8]}" for k in self.changed)
        job.log(f"Буде оновлено: {notes}")
        job.step("prepare", "done", notes)
        return True

    def _build(self) -> None:
        job = self.job
        job.step("build", "running")
        notes = []
        for kind in self.changed:
            needed = kind == "frontend" or self.image_rebuild or not self.p.image_exists(self.p.images[kind])
            if not needed:
                notes.append("бекенд: залежності не змінилися")
                continue
            with tempfile.TemporaryDirectory(prefix=f"hlv-{kind}-") as tmp:
                self.repos[kind].export(self.new[kind], Path(tmp))
                job.log(f"Збирання образу {LABELS[kind].lower()} ({self.new[kind][:8]}) — стара версія працює")
                self.p.build(kind, Path(tmp))
            self.built.add(kind)
            notes.append(f"{LABELS[kind].lower()}: зібрано")
        job.step("build", "done", "; ".join(notes))

    def _backup(self) -> None:
        job = self.job
        if "backend" not in self.changed:
            job.step("backup", "skipped", "оновлюється лише фронтенд")
            return
        job.step("backup", "running")
        # The scheduler writes archives; stopped first, the dump is the state
        # a restore returns to, without a run cut off half-way through it.
        self.p.stop(self.s.scheduler_service)
        self.scheduler_stopped = True
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.p.backup_dir() / f"pre-update-{stamp}.dump"
        job.log(f"Копія бази → {path}")
        try:
            self.p.dump(path)
        except CommandError:
            path.unlink(missing_ok=True)
            raise
        self.dump_path = path
        self.p.prune_backups(self.s.keep_backups)
        job.step("backup", "done", path.name)

    def _undo_preparation(self) -> None:
        for kind in self.built:
            self.p.drop_next(kind)
        if self.scheduler_stopped:
            try:
                self.p.recreate(self.s.scheduler_service)
            except CommandError as exc:
                self.job.log(f"Не вдалося знову запустити планувальник: {exc}")

    # ── the switch ────────────────────────────────────────────────────────
    def _switch(self) -> None:
        job = self.job
        job.step("switch", "running")
        for kind in sorted(self.built):
            self.p.swap_images(kind)
            self.swapped.add(kind)
        for kind in self.changed:
            self.repos[kind].merge_ff(self.new[kind])
            self.merged.add(kind)
        if "backend" in self.changed:
            self.p.recreate(self.s.backend_service)
        job.step("switch", "done")

    def _verify(self) -> None:
        job = self.job
        job.step("verify", "running")
        if "backend" in self.changed:
            ok, why = self.p.wait_ready(self.s.backend_service, self.s.backend_health_url, self.s.health_timeout)
            if not ok:
                self.p.logs(self.s.backend_service)
                raise RuntimeError(f"нова версія не запустилась: {why}")
            revisions = self.p.db_revisions()
            if self.new_heads and revisions != self.new_heads:
                raise RuntimeError(
                    f"база на ревізії {', '.join(sorted(revisions)) or '—'}, "
                    f"очікувалась {', '.join(sorted(self.new_heads))}"
                )
            self.p.recreate(self.s.scheduler_service)
            self.scheduler_stopped = False
            ok, why = self.p.stays_up(self.s.scheduler_service)
            if not ok:
                self.p.logs(self.s.scheduler_service)
                raise RuntimeError(why)
        if "frontend" in self.changed:
            self.p.recreate(self.s.frontend_service)
            ok, why = self.p.wait_ready(self.s.frontend_service, self.s.frontend_health_url, 60)
            if not ok:
                raise RuntimeError(f"фронтенд не запустився: {why}")
        job.step("verify", "done")

    # ── rollback ──────────────────────────────────────────────────────────
    def _rollback(self, reason: str) -> None:
        job = self.job
        job.step("rollback", "running")
        s = self.s
        db_restored = False
        try:
            if "backend" in self.changed:
                # Stopped first: a crash-looping backend reruns its migrations
                # on every start and would fight the downgrade.
                self.p.stop(s.backend_service, s.scheduler_service)
                if "backend" in self.merged:
                    db_restored = self._rollback_database()
            for kind in sorted(self.merged):
                self.repos[kind].reset_hard(self.old[kind])
            for kind in sorted(self.swapped):
                self.p.restore_image(kind)

            if "backend" in self.changed:
                self.p.recreate(s.backend_service)
                ok, why = self.p.wait_ready(s.backend_service, s.backend_health_url, s.health_timeout)
                if not ok and not db_restored and self.dump_path:
                    job.log(f"Попередня версія не запустилась ({why}) — відновлюю базу з копії")
                    self.p.stop(s.backend_service)
                    self.p.restore(self.dump_path)
                    db_restored = True
                    self.p.recreate(s.backend_service)
                    ok, why = self.p.wait_ready(s.backend_service, s.backend_health_url, s.health_timeout)
                if not ok:
                    self.p.logs(s.backend_service)
                    raise RuntimeError(f"попередня версія теж не запустилась: {why}")
                self.p.recreate(s.scheduler_service)
            if "frontend" in self.changed:
                self.p.recreate(s.frontend_service)
                ok, why = self.p.wait_ready(s.frontend_service, s.frontend_health_url, 60)
                if not ok:
                    raise RuntimeError(f"попередній фронтенд не запустився: {why}")
        except Exception as exc:  # noqa: BLE001
            job.step("rollback", "failed", str(exc))
            where = f" Копія бази: {self.dump_path}" if self.dump_path else ""
            job.finish(FAILED, f"Збій: {reason}; відкат не вдався: {exc}.{where}")
            return
        note = "з відновленням бази з копії" if db_restored else None
        job.step("rollback", "done", note)
        # The failed version's images lost their tag to the restored ones.
        self.p.prune()
        suffix = f" ({note})" if note else ""
        job.finish(ROLLED_BACK, f"Повернено попередню версію{suffix}. Причина: {reason}")

    def _rollback_database(self) -> bool:
        """Bring the schema back to what the old code expects. True if that
        took the dump — the data written since the backup is then gone."""
        current = self.p.db_revisions()
        if current == self.old_revisions:
            self.job.log("Структура бази не змінювалась — відкат бази не потрібен")
            return False
        if len(self.old_revisions) == 1:
            target = next(iter(self.old_revisions))
            try:
                self.job.log(f"alembic downgrade {target}")
                self.p.downgrade(target)
                if self.p.db_revisions() == self.old_revisions:
                    return False
                self.job.log("Після downgrade ревізія бази не та, що очікувалась")
            except CommandError as exc:
                self.job.log(f"downgrade не вдався: {exc}")
        if not self.dump_path:
            raise RuntimeError("структуру бази не повернуто, а копії бази немає")
        self.job.log(f"Відновлення бази з {self.dump_path.name}")
        self.p.restore(self.dump_path)
        return True
