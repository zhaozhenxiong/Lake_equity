"""Bounded top-level extraction from immutable, potentially GiB-sized reports."""
import hashlib
import json
import re


TOKENS = re.compile(rb'["\\{}\[\]]')
SPACE = b' \n\r\t'


def extract(path, keys, *, complete=False, check_cancel=None, progress=None, allowed_keys=None):
    """Skip large values without constructing them; hash only on full validation."""
    wanted = set(keys)
    found = {}
    digest = hashlib.sha256() if complete else None
    with path.open('rb') as handle:
        buffer, position, total = b'', 0, 0

        def fill():
            nonlocal buffer, position, total
            if check_cancel:
                check_cancel()
            buffer, position = handle.read(1024 * 1024), 0
            total += len(buffer)
            if digest is not None:
                digest.update(buffer)
            if progress:
                progress(total)
            return bool(buffer)

        def peek():
            nonlocal position
            while True:
                if position >= len(buffer) and not fill():
                    raise ValueError('Truncated learning report JSON')
                if buffer[position] not in SPACE:
                    return buffer[position]
                position += 1

        def expect(char):
            nonlocal position
            if peek() != ord(char):
                raise ValueError('Invalid learning report JSON delimiter')
            position += 1

        def value(capture):
            nonlocal position
            first = peek()
            pieces, size = [], 0
            nested = first in b'{['
            string_value = first == ord('"')
            depth, inside, escaped = 0, False, False
            while True:
                start = position
                finish = None
                if nested or string_value:
                    previous = position - 1
                    for match in TOKENS.finditer(buffer, position):
                        index, char = match.start(), match.group()[0]
                        if index > previous + 1:
                            escaped = False
                        previous = index
                        if inside:
                            if char == 92:
                                escaped = not escaped
                            else:
                                if char == 34 and not escaped:
                                    inside = False
                                    if string_value and depth == 0:
                                        finish = index + 1
                                        break
                                escaped = False
                        elif char == 34:
                            inside = True
                        elif char in b'{[':
                            depth += 1
                        elif char in b'}]':
                            depth -= 1
                            if depth == 0:
                                finish = index + 1
                                break
                    if previous != len(buffer) - 1:
                        escaped = False
                else:
                    match = re.search(rb'[,}\]\s]', buffer[position:])
                    if match:
                        finish = position + match.start()
                end = finish if finish is not None else len(buffer)
                if capture:
                    size += end - start
                    if size > 16 * 1024 * 1024:
                        raise ValueError('Learning retention metadata exceeds its 16MiB bound')
                    pieces.append(buffer[start:end])
                position = end
                if finish is not None:
                    return json.loads(b''.join(pieces)) if capture else None
                if not fill():
                    raise ValueError('Truncated learning report value')

        expect('{')
        while peek() != ord('}'):
            key = value(True)
            if not isinstance(key, str):
                raise ValueError('Learning report keys must be strings')
            if allowed_keys is not None and key not in allowed_keys:
                raise ValueError('Unknown field in the bounded retention consumer contract: '+key)
            expect(':')
            item = value(key in wanted)
            if key in wanted:
                if key in found:
                    raise ValueError('Duplicate retention metadata field')
                found[key] = item
            if not complete and wanted <= found.keys():
                return found, None
            if peek() == ord('}'):
                break
            expect(',')
        expect('}')
        if buffer[position:].strip():
            raise ValueError('Trailing learning report content')
        while fill():
            if buffer.strip():
                raise ValueError('Trailing learning report content')
        if not wanted <= found.keys():
            raise ValueError('Learning report retention metadata is incomplete')
        return found, digest.hexdigest() if digest is not None else None
