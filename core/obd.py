"""Conservative ELM327 decoding. Unknown/partial frames remain in the raw trace."""
import re


# PID: label, unit, minimum bytes, decoding function
PIDS = {
    '01': ('Reported emissions DTC count', 'codes', 4, lambda b: b[0] & 127),
    '04': ('Calculated engine load', '%', 1, lambda b: b[0] * 100 / 255),
    '05': ('Coolant temperature', '°C', 1, lambda b: b[0] - 40),
    '06': ('Short-term fuel trim · bank 1', '%', 1, lambda b: (b[0] - 128) * 100 / 128),
    '07': ('Long-term fuel trim · bank 1', '%', 1, lambda b: (b[0] - 128) * 100 / 128),
    '08': ('Short-term fuel trim · bank 2', '%', 1, lambda b: (b[0] - 128) * 100 / 128),
    '09': ('Long-term fuel trim · bank 2', '%', 1, lambda b: (b[0] - 128) * 100 / 128),
    '0B': ('Intake manifold pressure', 'kPa', 1, lambda b: b[0]),
    '0C': ('Engine speed', 'rpm', 2, lambda b: (b[0] * 256 + b[1]) / 4),
    '0D': ('Vehicle speed', 'km/h', 1, lambda b: b[0]),
    '0E': ('Ignition timing advance', '°', 1, lambda b: b[0] / 2 - 64),
    '0F': ('Intake air temperature', '°C', 1, lambda b: b[0] - 40),
    '10': ('Mass air flow', 'g/s', 2, lambda b: (b[0] * 256 + b[1]) / 100),
    '11': ('Throttle position', '%', 1, lambda b: b[0] * 100 / 255),
    '1F': ('Time since engine start', 's', 2, lambda b: b[0] * 256 + b[1]),
    '21': ('Distance with MIL illuminated', 'km', 2, lambda b: b[0] * 256 + b[1]),
    '2F': ('Fuel level', '%', 1, lambda b: b[0] * 100 / 255),
    '30': ('Warm-ups since codes cleared', 'cycles', 1, lambda b: b[0]),
    '31': ('Distance since codes cleared', 'km', 2, lambda b: b[0] * 256 + b[1]),
    '33': ('Barometric pressure', 'kPa', 1, lambda b: b[0]),
    '42': ('Control module voltage', 'V', 2, lambda b: (b[0] * 256 + b[1]) / 1000),
    '46': ('Ambient temperature', '°C', 1, lambda b: b[0] - 40),
    '5B': ('Hybrid battery remaining life · PID 5B', '%', 1, lambda b: b[0] * 100 / 255),
    '5C': ('Engine oil temperature', '°C', 1, lambda b: b[0] - 40),
}


def catalog():
    return [{'pid': p, 'label': d[0], 'unit': d[1]} for p, d in PIDS.items()] + [
        {'pid': 'RV', 'label': 'Adapter supply voltage', 'unit': 'V'}]


def supply_voltage(raw):
    match = re.search(r'(?:^|[\r\n])\s*(\d{1,2}(?:\.\d{1,3})?)V\s*(?:$|[\r\n])', raw, re.I)
    if not match:
        return []
    return [{'ecu': 'adapter', 'pid': 'RV', 'label': 'Adapter supply voltage', 'unit': 'V', 'value': float(match[1])}]


def payloads(raw, protocol='6'):
    """ATH1 + CAF1 responses. Reassemble ISO-TP independently for every ECU.

    CAN without spaces is supported. Legacy ISO/KWP/J1850 headers are three
    bytes, followed by payload and a checksum. The transport always enables
    headers and spaces; ambiguous/malformed responses are not guessed.
    """
    normalized = protocol.upper()[1:] if len(protocol) > 1 and protocol.upper().startswith('A') else protocol.upper()
    can = normalized in ('6', '7', '8', '9')
    out, pending = [], {}
    for line in re.split(r'[\r\n>]+', raw.upper()):
        line = line.strip()
        if not line or not re.fullmatch(r'[0-9A-F ]+', line):
            continue
        compact = line.replace(' ', '')
        if can:
            # ATH1 emits a three-nibble (11 bit) or eight-nibble (29 bit) ID.
            width = 3 if len(compact) % 2 else 8
            if len(compact) <= width or (width == 8 and len(compact) < 12):
                continue
            ecu, body = compact[:width], compact[width:]
            if len(body) % 2:
                continue
            b = list(bytes.fromhex(body))
            if not b:
                continue
            kind = b[0] >> 4
            if kind == 0:
                length = b[0] & 15
                if 0 < length <= len(b) - 1:
                    out.append({'ecu': ecu, 'data': b[1:1 + length]})
            elif kind == 1 and len(b) >= 3:
                length = (b[0] & 15) * 256 + b[1]
                pending[ecu] = {'length': length, 'data': b[2:], 'next': 1}
            elif kind == 2 and ecu in pending:
                item = pending[ecu]
                if b[0] & 15 != item['next']:
                    del pending[ecu]
                    continue
                item['next'] = (item['next'] + 1) % 16
                item['data'].extend(b[1:])
                if len(item['data']) >= item['length']:
                    out.append({'ecu': ecu, 'data': item['data'][:item['length']]})
                    del pending[ecu]
            elif kind in (4, 5, 6, 7):
                # Some CAF1 adapters strip PCI but retain CAN headers.
                out.append({'ecu': ecu, 'data': b})
        elif len(compact) >= 10 and len(compact) % 2 == 0:
            b = list(bytes.fromhex(compact))
            out.append({'ecu': compact[:6], 'data': b[3:-1]})
    return out


def supported(raw, base=0, protocol='6'):
    result = {}
    for row in payloads(raw, protocol):
        b = row['data']
        if len(b) >= 6 and b[:2] == [0x41, base]:
            bits = int.from_bytes(bytes(b[2:6]), 'big')
            result[row['ecu']] = [f'{base + n:02X}' for n in range(1, 33) if bits & (1 << (32 - n))]
    return result


def dtcs(raw, mode, protocol='6'):
    found = []
    for row in payloads(raw, protocol):
        b = row['data']
        if not b or b[0] != int(mode, 16) + 0x40:
            continue
        # SAE J1979 over CAN includes a DTC count; legacy protocols do not.
        normalized = protocol.upper()[1:] if len(protocol) > 1 and protocol.upper().startswith('A') else protocol.upper()
        can = normalized in ('6', '7', '8', '9')
        if can:
            if len(b) < 2 or len(b) < 2 + b[1] * 2:
                continue
            pairs = b[2:2 + b[1] * 2]
        else:
            pairs = b[1:]
        for i in range(0, len(pairs) - 1, 2):
            a, c = pairs[i:i + 2]
            if a == c == 0:
                continue
            code = f'{"PCBU"[a >> 6]}{(a >> 4) & 3}{a & 15:X}{c:02X}'
            found.append({'code': code, 'ecu': row['ecu'], 'status': {'03': 'stored', '07': 'pending', '0A': 'permanent'}[mode], 'definition': None})
    return found


def readings(raw, pid, protocol='6'):
    if pid not in PIDS:
        return []
    label, unit, size, decode = PIDS[pid]
    result = []
    for row in payloads(raw, protocol):
        b = row['data']
        if len(b) >= 2 + size and b[:2] == [0x41, int(pid, 16)]:
            result.append({'ecu': row['ecu'], 'pid': pid, 'label': label, 'unit': unit, 'value': round(decode(b[2:]), 3)})
    return result


def vin(raw, protocol='6', *, require_consensus=False):
    candidates = []
    for row in payloads(raw, protocol):
        b = row['data']
        if b[:2] == [0x49, 0x02] and len(b) >= 20:
            value = bytes(b[3:20]).decode('ascii', errors='replace')
            if re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', value):
                if not require_consensus:
                    return value
                candidates.append(value)
    if len(set(candidates)) > 1:
        raise ValueError('ECUs reported conflicting VINs. Check the vehicle and verify again before assignment.')
    return candidates[0] if candidates else None


def monitor(raw, protocol='6'):
    result = []
    for row in payloads(raw, protocol):
        b = row['data']
        if b[:2] == [0x41, 0x01] and len(b) >= 6:
            result.append({'ecu': row['ecu'], 'mil': bool(b[2] & 128), 'dtcCount': b[2] & 127, 'readinessBytes': bytes(b[3:6]).hex(' ').upper()})
    return result
