from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .catalog import build_reply, find_products, parse_catalog


class InstanceLock:
    def __init__(self, directory: Path):
        self.file = open(directory / 'instance.lock', 'a+b')

    def __enter__(self):
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.write(b'0')
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ValueError('Другой процесс уже использует эту папку DATA_DIR') from exc
        return self

    def __exit__(self, *args):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description='Отдельный Telegram-бот для ответов по наличию')
    parser.add_argument('command', choices=['login', 'session-env', 'run', 'demo', 'check-catalog', 'check-price'])
    parser.add_argument('file', nargs='?')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    try:
        if args.command == 'check-price':
            from .prices import parse_price_feed
            if not args.file:
                raise ValueError('Укажи путь к текстовому файлу прайса')
            snapshot = parse_price_feed(Path(args.file).read_text(encoding='utf-8-sig'))
            print(f'Товарных блоков: {snapshot.raw_count}; предложений без дублей: {len(snapshot.products)}; '
                  f'точных дублей: {snapshot.duplicates}; полный прайс: {snapshot.complete}; '
                  f'спорных строк: {sum(bool(p.warnings) for p in snapshot.products)}')
            return
        if args.command in {'demo', 'check-catalog'}:
            path = Path(args.file) if args.file else Path(__file__).resolve().parent.parent / 'examples' / 'catalog.txt'
            products = parse_catalog(path.read_text(encoding='utf-8-sig'))
            print(f'Список прочитан: {len(products)} позиций. Данные и цены — учебные.' if args.command == 'demo' else f'Список прочитан: {len(products)} позиций.')
            if args.command == 'demo':
                for text in ('Нужен iPhone 17', 'Предложите\niPhone 17 Pro Max 256 silver sim',
                             'продам iPhone 17', 'MDE44', 'iPhone 17 Pro Max 512 blue esim'):
                    classification, found = find_products(text, products)
                    print(f'\nЗапрос: {text!r}\nТип: {classification}; совпадений: {len(found)}')
                    print(build_reply(found) if found else 'Ответ не отправляется.')
            return
        from .app import Config, login, run
        config = Config.from_env(login_only=args.command in {'login', 'session-env'})
        with InstanceLock(config.data_dir):
            if args.command == 'session-env':
                from .sessions import export_session
                path = export_session(config.data_dir)
                print(f'Переменная для Railway сохранена в {path}. Скопируй её в Variables; файл не публикуй.')
                return
            asyncio.run(login(config) if args.command == 'login' else run(config))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
