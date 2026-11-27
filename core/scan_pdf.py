"""Printable diagnostic summary built from the recorded scan and its references."""

from datetime import datetime, timezone
from io import BytesIO
from textwrap import shorten
from xml.sax.saxutils import escape


CATEGORIES = (('stored', 'Stored', '03'), ('pending', 'Pending', '07'),
              ('permanent', 'Permanent', '0A'), ('other', 'Other / unclassified', None))


def describe_code(entry, references):
    matches = [ref for ref in references if ref['code'] == entry['code']
               and (not ref.get('ecu') or ref['ecu'].casefold() == (entry.get('ecu') or '').casefold())]
    if entry.get('definition'):
        return entry['definition'], 'Definition supplied by the scan source.'
    if matches:
        match = matches[0]
        return match['description'], ' · '.join(filter(None, (match.get('origin'), match.get('reference'))))
    return ('No definition available for this code and module. Check the applicable service information.', '')


def summary_pdf(scan, references):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    green = colors.HexColor('#294c39')
    ink = colors.HexColor('#183026')
    muted = colors.HexColor('#4d5b53')
    line = colors.HexColor('#d4ded7')
    styles = {
        'title': ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=27, leading=32, textColor=ink, spaceAfter=6),
        'section': ParagraphStyle('section', fontName='Helvetica-Bold', fontSize=13, leading=17, textColor=green,
                                  spaceBefore=13, spaceAfter=5, keepWithNext=True),
        'code': ParagraphStyle('code', fontName='Helvetica-Bold', fontSize=10, leading=14, textColor=ink),
        'body': ParagraphStyle('body', fontName='Helvetica', fontSize=10, leading=14, textColor=ink, spaceAfter=4),
        'small': ParagraphStyle('small', fontName='Helvetica', fontSize=8.5, leading=12, textColor=muted),
    }

    def para(text, kind='body'):
        safe = ''.join(char if char.isprintable() else ' ' for char in str(text))
        return Paragraph(escape(safe), styles[kind])

    def table(rows, widths):
        result = Table(rows, colWidths=widths, repeatRows=1, hAlign='LEFT')
        result.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e7f0e9')),
            ('LINEBELOW', (0, 0), (-1, -1), 0.55, line),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        return result

    created = datetime.fromisoformat(scan['createdAt']).astimezone(timezone.utc)
    timestamp = created.strftime('%b %d, %Y at %H:%M:%S UTC')
    vehicle = ' '.join(str(scan['vehicle'][key]) for key in ('year', 'make', 'model') if scan['vehicle'].get(key))
    status = scan.get('scanStatus')
    result = 'Demonstration' if scan.get('demo') else {
        'complete': 'Completed', 'partial': 'Partial coverage', 'failed': 'Scan attempt failed',
    }.get(status if isinstance(status, str) else None, 'Saved scan')
    story = [para('Diagnostic scan summary', 'title'), para(vehicle or 'Unidentified vehicle', 'code'),
             para(f"{timestamp}  |  {result}  |  VIN {scan.get('vin') or 'unavailable'}"),
             para(scan.get('scope') or scan.get('source') or 'Coverage depends on responses recorded in the scan.', 'small')]

    groups = {key: [] for key, _, _ in CATEGORIES}
    for entry in scan['codes']:
        status = (entry.get('status') or '').lower()
        groups[status if status in groups else 'other'].append(entry)
    coverage = scan.get('codeCoverage')
    if not isinstance(coverage, dict):
        coverage = {}
    rows = [[para(text, 'code') for text in ('Code category', 'Reported', 'Recorded coverage')]]
    for key, label, mode in CATEGORIES:
        if not mode and not groups[key]:
            continue
        responding = coverage.get(mode)
        if isinstance(responding, list) and responding:
            detail = 'Responding ECUs: ' + ', '.join(map(str, responding))
        elif mode in coverage:
            detail = 'No confirmed response'
        else:
            detail = 'Coverage not recorded'
        rows.append([para(label), para(len(groups[key])), para(detail)])
    story.extend([para('Scan categories', 'section'), table(rows, [153, 70, 293])])

    story.append(para('Reported codes', 'section'))
    for key, label, _ in CATEGORIES:
        for entry in groups[key]:
            blurb, source = describe_code(entry, references)
            category = (entry.get('status') or 'Unclassified') if key == 'other' else label
            ecu = f"ECU {entry['ecu']}" if entry.get('ecu') else None
            title = ' · '.join(filter(None, (entry['code'], category, ecu)))
            block = [para(title, 'code'), para(shorten(blurb, 360, placeholder='...'))]
            if source:
                block.append(para(shorten(source, 220, placeholder='...'), 'small'))
            story.append(KeepTogether([*block, Spacer(1, 7)]))
    if not scan['codes']:
        story.append(para('No code result is available for this attempt.' if result == 'Scan attempt failed'
                          else 'No codes were recorded. Review the scan coverage before interpreting this result.'))

    warnings = scan.get('warnings', [])
    if warnings:
        story.append(para('Scan notes', 'section'))
        story.extend(para(warning) for warning in warnings)

    monitors = scan.get('monitors', [])
    if monitors:
        story.append(para('Monitor status', 'section'))
        for monitor in monitors:
            lamp = 'On' if monitor.get('mil') else 'Off' if monitor.get('mil') is False else 'Unknown'
            count = monitor.get('dtcCount', 'Unknown')
            story.append(para(f"ECU {monitor.get('ecu') or 'unavailable'} · Warning light {lamp} · Reported code count {count}"))
    readings = scan.get('readings', [])
    if readings:
        story.append(para('Scan-time readings', 'section'))
        rows = [[para(text, 'code') for text in ('Reading', 'Value', 'ECU')]]
        for item in readings:
            reading = item.get('label') or item.get('pid') or 'Reading'
            value = f"{item['value']:g} {item.get('unit') or ''}".strip()
            rows.append([para(reading), para(value), para(item.get('ecu') or 'Unavailable')])
        story.append(table(rows, [250, 166, 100]))

    def frame(canvas, doc):
        width, height = doc.pagesize
        canvas.setStrokeColor(line)
        canvas.line(42, height - 49, width - 42, height - 49)
        canvas.line(42, 46, width - 42, 46)
        canvas.setFillColor(green)
        canvas.setFont('Helvetica-Bold', 11)
        canvas.drawString(42, height - 39, 'ProActive')
        canvas.setFillColor(muted)
        canvas.setFont('Helvetica', 8)
        canvas.drawString(42, 31, 'Scan ID ' + scan['id'])
        canvas.drawRightString(width - 42, 31, f'Page {doc.page}')

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=letter, topMargin=65, bottomMargin=60,
                                 leftMargin=42, rightMargin=42, title='ProActive diagnostic scan summary',
                                 author='ProActive')
    document.build(story, onFirstPage=frame, onLaterPages=frame)
    return output.getvalue()
