import { deflateSync } from 'node:zlib';

// Entirely invented UI examples. These values are never written to a live API or gold set.
export const project = '00000000-0000-4000-8000-000000000001';
export const populated = '00000000-0000-4000-8000-000000000101';
export const empty = '00000000-0000-4000-8000-000000000102';
export const revision = '00000000-0000-4000-8000-000000000201';
export const packages = [
  { id: populated, project_id: project, current_revision_id: revision, current_revision_number: 1, state: 'AWAITING_REVIEW', vendor: 'SYNTHETIC UI QA · Sample cabinet review', created_at: '2026-10-03T10:00:00Z' },
  { id: empty, project_id: project, current_revision_id: '00000000-0000-4000-8000-000000000202', current_revision_number: 1, state: 'CREATED', vendor: 'SYNTHETIC UI QA · Empty review', created_at: '2026-10-03T10:00:00Z' },
];

const rules = ['CT-DEPTH-001', 'CT-SINK-OFFSET-FRONT-001', 'CAB-FILLER-001', 'CT-SINK-CUTOUT-WIDTH-001'];
const outcomes = ['FAIL', 'PASS', 'REVIEW_REQUIRED', 'NOT_FOUND'];
export const findings = rules.map((rule_id, i) => ({
  id: `00000000-0000-4000-8000-00000000030${i + 1}`, rule_id, outcome: outcomes[i], severity: 'FLAG',
  check_run_id: 'synthetic-run', check_type: 'synthetic-ui-example', created_at: '2026-10-03T10:00:00Z', engine_version: 'SYNTHETIC_UI_QA',
  package_revision_id: revision, revision_number: 1, parameter_set_versions: {}, product_type: 'COUNTERTOP',
  rule_snapshot_hash: 'synthetic-not-production', rule_snapshot_id: 'synthetic', rule_version: '1', reviewer_action: null,
}));
const evidence = (role, id) => ({
  canonical_observation_id: id, document_version_id: `synthetic-${role.toLowerCase()}-document`, document_role: role,
  page_index: 0, page_id: `synthetic-${role.toLowerCase()}-page`, polygon: [['20', '30'], ['240', '30'], ['240', '120'], ['20', '120']],
  semantic_type: 'countertop_depth', authority: 'SYNTHETIC_UI_FIXTURE', coordinate_space: 'pdf_points', crop_uri: `synthetic://${id}`,
});
export const chains = Object.fromEntries(findings.map((finding, i) => [finding.id, {
  finding_id: finding.id, outcome: finding.outcome, severity: 'FLAG', engine_version: 'SYNTHETIC_UI_QA', parameter_versions: {},
  rule_snapshot: { id: 'synthetic', version: '1', hash: 'synthetic', rule_id: finding.rule_id },
  operands: i === 0 ? [
    { name: 'approved_depth', numerator: '51', denominator: '2', unit: 'in', evidence_status: 'APPROVED', evidence: evidence('ARCH', 'synthetic-arch-crop') },
    { name: 'vendor_depth', numerator: '101', denominator: '4', unit: 'in', evidence_status: 'APPROVED', evidence: evidence('SHOP', 'synthetic-shop-crop') },
  ] : i === 1 ? [{ name: 'front_offset', numerator: '4', denominator: '1', unit: 'in', evidence_status: 'APPROVED', evidence: null }] : [],
  trace: i < 2 ? { kind: 'calculation', operation: 'equals', operands: i === 0 ? [
    { name: 'approved_depth', value: '51/2 in', source: 'ARCH' }, { name: 'vendor_depth', value: '101/4 in', source: 'SHOP' },
  ] : [{ name: 'front_offset', value: '4 in', source: 'USER_INPUT' }], comparison: i === 0 ? '101/4 in != 51/2 in' : '4 in == 4 in' }
    : { kind: 'abstention', cause: 'missing_input', reason: i === 2 ? 'Synthetic example: filler bound requires a reviewer decision.' : 'Synthetic example: no sink cutout width was supplied.' },
}]));

export const needed = {
  quantities: [
    { key: 'SHOP:countertop_depth', semantic_type: 'countertop_depth', source: 'SHOP', many: false, consumers: [{ rule_id: rules[0], input_name: 'vendor_depth' }], categories: [] },
    { key: 'ARCH:countertop_depth', semantic_type: 'countertop_depth', source: 'ARCH', many: false, consumers: [{ rule_id: rules[0], input_name: 'approved_depth' }], categories: [] },
    { key: 'SHOP:filler_width', semantic_type: 'filler_width', source: 'SHOP', many: true, consumers: [{ rule_id: rules[2], input_name: 'filler_width' }], categories: [] },
  ],
  confirmed_readings: [{ key: 'ARCH:countertop_depth', source: 'ARCH', semantic_type: 'countertop_depth', value: '25 1/2 in', qualification: 'reviewer_confirmed' }],
  proposed_readings: [], parameters: [], discriminators: [], rules_published: 4, revision_state: 'AWAITING_REVIEW', still_reading: false,
};
export const candidates = [{ candidate_id: 'synthetic-shop-crop', confidence: '0.96', crop_key: 'synthetic-only', page_index: 0, raw_text: '25 1/4 in', source: 'SHOP', source_refusal: null, value: '25 1/4 in' }];

// A dependency-free, genuine PNG: a clearly synthetic dimension sketch for image rendering QA.
function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) { crc ^= byte; for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(name, data) {
  const type = Buffer.from(name), size = Buffer.alloc(4), crc = Buffer.alloc(4);
  size.writeUInt32BE(data.length); crc.writeUInt32BE(crc32(Buffer.concat([type, data])));
  return Buffer.concat([size, type, data, crc]);
}
export function cropPng(shop) {
  const width = 640, height = 230, stride = width * 3 + 1;
  const pixels = Buffer.alloc(stride * height, 255);
  for (let y = 0; y < height; y++) pixels[y * stride] = 0;
  function point(x, y) { if (x < 0 || y < 0 || x >= width || y >= height) return; const at = y * stride + 1 + x * 3; pixels.fill(35, at, at + 3); }
  function line(x1, y1, x2, y2) {
    const steps = Math.max(Math.abs(x2 - x1), Math.abs(y2 - y1));
    for (let i = 0; i <= steps; i++) point(Math.round(x1 + (x2 - x1) * i / steps), Math.round(y1 + (y2 - y1) * i / steps));
  }
  const glyphs = {
    '0':['111','101','101','101','111'], '1':['010','110','010','010','111'], '2':['111','001','111','100','111'], '4':['101','101','111','001','001'], '5':['111','100','111','001','111'],
    '/':['001','001','010','100','100'], ' ':['000','000','000','000','000'], I:['111','010','010','010','111'], N:['1001','1101','1011','1001','1001'],
    S:['111','100','111','001','111'], Y:['101','101','010','010','010'], T:['111','010','010','010','010'], H:['101','101','111','101','101'], E:['111','100','111','100','111'], C:['111','100','100','100','111'],
  };
  function text(value, x, y, scale) {
    for (const character of value) { const glyph = glyphs[character] ?? glyphs[' ']; glyph.forEach((row, yy) => [...row].forEach((bit, xx) => { if (bit === '1') for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++) point(x + xx * scale + dx, y + yy * scale + dy); })); x += (glyph[0].length + 1) * scale; }
  }
  text('SYNTHETIC', 225, 20, 4);
  text(shop ? '25 1/4 IN' : '25 1/2 IN', 210, 65, 6);
  line(60, 118, 580, 118); line(60, 106, 60, 190); line(580, 106, 580, 190);
  line(60, 118, 75, 110); line(60, 118, 75, 126); line(580, 118, 565, 110); line(580, 118, 565, 126);
  line(60, 165, 580, 165); line(60, 190, 580, 190);
  const header = Buffer.alloc(13); header.writeUInt32BE(width); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
}

export function samplePdf(redline = false) {
  const stream = `BT /F1 18 Tf 50 740 Td (SYNTHETIC UI QA - ${redline ? 'Redline download' : 'Report download'}) Tj 0 -35 Td /F1 11 Tf (Frontend download test only. No client drawing or actual review.) Tj ET\n${redline ? '0.5 0 0 RG 2 w 50 570 350 90 re S' : ''}`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ];
  let output = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(Buffer.byteLength(output)); output += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const start = Buffer.byteLength(output);
  output += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(output);
}

// Minimal real XLSX ZIP, for testing the workbook download link without a backend export.
export function sampleWorkbook() {
  const files = {
    '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    '_rels/.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    'xl/workbook.xml': '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic UI QA" sheetId="1" r:id="rId1"/></sheets></workbook>',
    'xl/_rels/workbook.xml.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    'xl/worksheets/sheet1.xml': '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>SYNTHETIC UI QA ONLY</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Frontend download test. No client data or actual review.</t></is></c></row></sheetData></worksheet>',
  };
  const local = [], central = []; let offset = 0;
  for (const [path, content] of Object.entries(files)) {
    const name = Buffer.from(path), bytes = Buffer.from(content), crc = crc32(bytes);
    const header = Buffer.alloc(30); header.writeUInt32LE(0x04034b50); header.writeUInt16LE(20, 4); header.writeUInt32LE(crc, 14); header.writeUInt32LE(bytes.length, 18); header.writeUInt32LE(bytes.length, 22); header.writeUInt16LE(name.length, 26);
    const directory = Buffer.alloc(46); directory.writeUInt32LE(0x02014b50); directory.writeUInt16LE(20, 4); directory.writeUInt16LE(20, 6); directory.writeUInt32LE(crc, 16); directory.writeUInt32LE(bytes.length, 20); directory.writeUInt32LE(bytes.length, 24); directory.writeUInt16LE(name.length, 28); directory.writeUInt32LE(offset, 42);
    local.push(header, name, bytes); central.push(directory, name); offset += header.length + name.length + bytes.length;
  }
  const end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50); end.writeUInt16LE(Object.keys(files).length, 8); end.writeUInt16LE(Object.keys(files).length, 10); end.writeUInt32LE(central.reduce((size, entry) => size + entry.length, 0), 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, ...central, end]);
}
