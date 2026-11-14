#!/usr/bin/env python3
"""Local node inventory. Identity and certificates remain stable across display renames."""
import argparse
import contextlib
import json
from pathlib import Path
import re
import sys
import tempfile
import os

from package import node_name


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.node-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=True, indent=2)
            f.write('\n')
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Nodes:
    def __init__(self, root):
        self.root = Path(root)

    def paths(self, key):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', key):
            raise ValueError('节点内部编号无效。')
        return [self.root / 'prometheus' / kind / (key + '.json')
                for kind in ('https-targets', 'targets')]

    def inventory(self):
        records = {}
        for kind in ('targets', 'https-targets'):
            for path in (self.root / 'prometheus' / kind).glob('*.json'):
                key = path.stem
                self.paths(key)
                try:
                    values = json.loads(path.read_text())
                    group = values[0] if values else {}
                    records[key] = {'id': key, 'label': group.get('labels', {}).get('node', key),
                                    'address': ', '.join(group.get('targets', [])), 'paused': False}
                except (ValueError, TypeError, KeyError, AttributeError):
                    records[key] = {'id': key, 'label': key, 'address': '配置损坏，请重新部署', 'paused': False}
        for path in (self.root / 'nodes').glob('*.json'):
            item = json.loads(path.read_text())
            key = path.stem
            self.paths(key)
            # Registry is also the inventory for paused nodes.
            if key not in records and not item.get('paused'):
                continue
            current = records.get(key, {})
            records[key] = {**current, **item, 'id': key,
                            'address': current.get('address', item.get('address', ''))}
        return sorted(records.values(), key=lambda v: (v['label'].casefold(), v['id']))

    def resolve(self, value):
        items = self.inventory()
        # Internal IDs take priority; display names cannot shadow another ID.
        for item in items:
            if item['id'] == value:
                return item['id']
        matches = [item['id'] for item in items if item['label'] == value]
        if len(matches) > 1:
            raise ValueError('存在同名节点，请从列表选择。')
        return matches[0] if matches else node_name(value)['name']

    def get(self, key):
        return next((v for v in self.inventory() if v['id'] == key), None)

    def rename(self, key, label):
        label = node_name(label)['display_name']
        item = self.get(key)
        if item is None:
            raise ValueError('节点不存在。')
        if any(v['id'] != key and label in (v['id'], v['label']) for v in self.inventory()):
            raise ValueError('该名称已被其他节点使用。')
        if any(v['id'] != key and node_name(label)['name'] == v['id'] for v in self.inventory()):
            raise ValueError('该名称与其他节点的内部编号冲突。')
        with self.transaction(key):
            for path in self.paths(key) + self.paused_paths(key):
                if path.exists():
                    value = json.loads(path.read_text())
                    for group in value:
                        group.setdefault('labels', {})['node'] = label
                    write(path, value, 0o644)
            write(self.root / 'nodes' / (key + '.json'), {**item, 'label': label})

    def paused_paths(self, key):
        return [self.root / 'paused-targets' / path.parent.name / path.name for path in self.paths(key)]

    @contextlib.contextmanager
    def transaction(self, key):
        paths = self.paths(key) + self.paused_paths(key) + [self.root / 'nodes' / (key + '.json')]
        old = {p: (p.read_bytes(), p.stat().st_mode & 0o777) if p.exists() else None for p in paths}
        try:
            yield
        except Exception:
            for p, snapshot in old.items():
                if snapshot is None:
                    p.unlink(missing_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(snapshot[0])
                    p.chmod(snapshot[1])
            raise

    def pause(self, key, paused):
        item = self.get(key)
        if item is None:
            raise ValueError('节点不存在。')
        if bool(item.get('paused')) == paused:
            return
        with self.transaction(key):
            for active, saved in zip(self.paths(key), self.paused_paths(key)):
                src, dst = (active, saved) if paused else (saved, active)
                if src.exists():
                    if dst.exists():
                        raise ValueError('目标配置冲突，未覆盖。')
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.replace(dst)
            write(self.root / 'nodes' / (key + '.json'), {**item, 'paused': paused})

    def forget(self, key):
        for path in self.paths(key) + self.paused_paths(key):
            path.unlink(missing_ok=True)
        (self.root / 'deploy' / (key + '.json')).unlink(missing_ok=True)
        (self.root / 'nodes' / (key + '.json')).unlink(missing_ok=True)

    def relabel(self, key):
        if not (self.root / 'nodes' / (key + '.json')).exists():
            return
        item = self.get(key)
        if item:
            self.rename(key, item['label'])
            # Redeploy explicitly resumes a previously paused node.
            if item.get('paused'):
                for path in self.paused_paths(key):
                    path.unlink(missing_ok=True)
                write(self.root / 'nodes' / (key + '.json'), {**item, 'paused': False})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('action', choices=['list', 'resolve', 'rename', 'pause', 'resume', 'forget', 'relabel'])
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    nodes = Nodes(args.root)
    if args.action == 'list':
        print(json.dumps(nodes.inventory(), ensure_ascii=True))
    elif args.action == 'resolve':
        print(nodes.resolve(args.values[0]))
    elif args.action == 'rename':
        nodes.rename(*args.values)
    elif args.action in ('pause', 'resume'):
        nodes.pause(args.values[0], args.action == 'pause')
    else:
        getattr(nodes, args.action)(args.values[0])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, IndexError) as e:
        print('节点管理失败：' + str(e), file=sys.stderr)
        sys.exit(1)
