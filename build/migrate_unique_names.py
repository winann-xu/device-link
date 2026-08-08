"""
模块：migrate_unique_names.py
功能：旧库设备名去重迁移脚本
     在添加 UNIQUE INDEX 之前，清理已有数据库中的重复设备名。
     策略：同名设备保留 ID 最小的那条，其余追加 _dup<N> 后缀。

用法：
    python build/migrate_unique_names.py [db_path]

作者：Claude
创建日期：2026-08-08
"""
import os
import sys
import sqlite3
import argparse
from pathlib import Path
from collections import Counter

# 项目根
sys.path.insert(0, str(Path(__file__).parent.parent))


def find_duplicates(conn: sqlite3.Connection) -> dict:
    """查找所有重名设备，返回 {name: [id1, id2, ...]}。"""
    rows = conn.execute(
        "SELECT name, GROUP_CONCAT(id) as ids, COUNT(*) as cnt "
        "FROM devices GROUP BY name HAVING cnt > 1"
    ).fetchall()
    dupes = {}
    for row in rows:
        ids = [int(x) for x in row['ids'].split(',')]
        dupes[row['name']] = sorted(ids)
    return dupes


def migrate(conn: sqlite3.Connection, dry_run: bool = False):
    """
    重命名重复设备：保留最小 ID 的原名，其余加后缀。

    参数:
        conn: 数据库连接
        dry_run: 为 True 时仅打印计划，不实际修改
    """
    dupes = find_duplicates(conn)

    if not dupes:
        print("未发现重名设备，无需迁移。")
        return

    print(f"发现 {len(dupes)} 组重名设备：")
    for name, ids in dupes.items():
        print(f"  {name}: {len(ids)} 条记录, ID={ids}")

    if dry_run:
        print("\n[dry-run] 以上重名将被修改，未实际执行。")
        return

    total_renamed = 0
    for name, ids in dupes.items():
        keeper = ids[0]  # 保留最小的 ID
        for dup_id in ids[1:]:
            new_name = f"{name}_dup{dup_id}"
            conn.execute(
                "UPDATE devices SET name=? WHERE id=?",
                (new_name, dup_id)
            )
            total_renamed += 1
            print(f"  重命名: ID={dup_id} {name} → {new_name}")

    conn.commit()
    print(f"\n迁移完成：{total_renamed} 条记录重命名，{len(dupes)} 组去重。")

    # 验证无重复
    remaining = find_duplicates(conn)
    if remaining:
        print(f"警告：仍有 {len(remaining)} 组重名！请手动处理。")
    else:
        print("验证通过：无重名设备，可以安全添加 UNIQUE INDEX。")


def main():
    parser = argparse.ArgumentParser(description='DEVICE LINK 设备名去重迁移')
    parser.add_argument('db_path', nargs='?',
                        default='./data/device-link.db',
                        help='数据库路径（默认 ./data/device-link.db）')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅打印计划，不实际修改')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(__file__), '..', db_path)

    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在: {db_path}")
        print("如果这是首次运行则无需迁移——新库的 UNIQUE INDEX 会在 init_database 时直接创建。")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        migrate(conn, dry_run=args.dry_run)
        if not args.dry_run:
            # 添加 UNIQUE INDEX
            try:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_name_unique ON devices(name)")
                conn.commit()
                print("UNIQUE INDEX 已添加（如已存在则跳过）。")
            except sqlite3.OperationalError as e:
                print(f"添加索引失败: {e}")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
