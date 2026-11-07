"""Temp-папка: подготовка к докачке, чистка и перенос готовых файлов."""
import os
import shutil

from config import TEMP_KEEP_SUFFIX, TEMP_MAX_BYTES, load_temp_state, save_temp_state


def prepare_temp_folder(folder, queue_ids=None, signature=None):
    """Готовит папку temp.

    Удаляет всё, кроме .part. Оставляет .part только для видео из очереди и
    только если настройки скачивания не менялись. Если размер превышает
    TEMP_MAX_BYTES — удаляет самые старые .part. Возвращает число удалённых
    файлов.
    """
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        print("create temp folder error:", e)
        return 0
    try:
        entries = os.listdir(folder)
    except Exception as e:
        print("list temp folder error:", e)
        return 0

    removed = 0
    parts = []
    for entry in entries:
        full = os.path.join(folder, entry)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
            removed += 1
        elif entry.lower().endswith(TEMP_KEEP_SUFFIX):
            parts.append((entry, full))
        else:
            try:
                os.remove(full)
                removed += 1
            except Exception:
                pass
    if not parts:
        save_temp_state({})
        return removed

    state = load_temp_state()
    allowed = set(queue_ids or ())
    survivors = []
    for entry, full in parts:
        vid = entry.split(".")[0]
        # Сирота: видео больше не в очереди
        if vid and vid not in allowed:
            try:
                os.remove(full)
                removed += 1
            except Exception:
                pass
            continue
        # Сменились настройки скачивания — старый .part не подходит
        if signature is not None and state.get(vid) not in (None, signature):
            try:
                os.remove(full)
                removed += 1
            except Exception:
                pass
            continue
        survivors.append((vid, full))

    new_state = {}
    for vid, _full in survivors:
        new_state[vid] = signature if signature is not None else state.get(vid)
    save_temp_state(new_state)

    try:
        details = []
        total = 0
        for vid, full in survivors:
            try:
                size = os.path.getsize(full)
                details.append((os.path.getmtime(full), size, full))
                total += size
            except OSError:
                pass
        if total > TEMP_MAX_BYTES:
            details.sort(key=lambda x: x[0])
            for _mtime, size, full in details:
                if total <= TEMP_MAX_BYTES:
                    break
                try:
                    os.remove(full)
                    total -= size
                    removed += 1
                    # Запись в state тоже больше не нужна
                    new_state.pop(os.path.basename(full).split(".")[0], None)
                except OSError:
                    pass
            save_temp_state(new_state)
    except Exception:
        pass
    return removed


def move_to_target(path, date_folder, fname):
    """Переносит готовый файл из temp в целевую папку. Возвращает итоговый путь."""
    if not path or not os.path.exists(path):
        return path
    os.makedirs(date_folder, exist_ok=True)
    dest = os.path.join(date_folder, fname + os.path.splitext(path)[1])
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(date_folder, f"{fname} ({n}){os.path.splitext(path)[1]}")
        n += 1
    shutil.move(path, dest)
    return dest
