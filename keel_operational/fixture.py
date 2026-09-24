"""Export a synthetic browser fixture; no server, browser or account actions.

The fixture exposes scope and uploaded-file digests as visible text so a native
accessibility tool need not evaluate JavaScript. These page-reported values are
fixture observations, not platform attestations or general employer capabilities.
"""
import base64
import hashlib
import html
import os
from pathlib import Path

from keel_loki.actions import ActionGateway
from keel_loki.browser import fixture_html
from keel_loki.forms import validate_contract
from keel_muse.session import _validate_attachments
from tools.operational_inventory import directory_fd, safe_name
from .common import FLAGS, atomic_json, clone, digest, new_home, require_hash


_READOUT = r"""
document.getElementById('actual-origin').textContent = 'Observed browser origin: ' + location.origin;
for (const input of document.querySelectorAll('input[type=file]')) {
  const output = document.createElement('output');
  output.setAttribute('aria-live', 'polite');
  output.textContent = 'Attachment readback: no file selected';
  input.parentElement.appendChild(output);
  let revision = 0;
  // The form's earlier refresh handler clears conditional file inputs without
  // emitting change on that input. Clear its readout and fence pending hashes.
  function clearMissing() {
    if (input.files.length !== 1) {
      revision++;
      output.textContent = 'Attachment readback: no single file selected';
    }
  }
  document.addEventListener('input', clearMissing);
  document.addEventListener('change', clearMissing);
  input.addEventListener('change', async () => {
    const current = ++revision;
    output.textContent = 'Attachment readback: pending';
    if (input.files.length !== 1) {
      output.textContent = 'Attachment readback: no single file selected'; return;
    }
    const file = input.files[0];
    const metadata = {name:file.name, mime_type:file.type, size:file.size};
    try {
      const bytes = await file.arrayBuffer();
      const hash = await crypto.subtle.digest('SHA-256', bytes);
      metadata.sha256 = [...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join('');
    } catch (_) { metadata.sha256 = 'UNKNOWN'; }
    if (revision === current) output.textContent = 'Attachment readback: ' + JSON.stringify(metadata);
  });
}
"""


def _write(home, name, data):
    safe_name(name)
    if '/' in name:
        raise ValueError('flat_fixture_file_required')
    parent = directory_fd(home)
    try:
        fd = os.open(Path('/proc/self/fd') / str(parent) / name,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def export_fixture(home, plan, *, expected_plan_sha256):
    """Export only the independently pinned, explicitly synthetic plan fixture.

    The receiving host must serve it at the authorized origin and retain native
    tool permissions. Exporting an expired plan grants no runtime eligibility;
    qualification.run/verify independently revalidate all current pins/deadlines.
    """
    require_hash(expected_plan_sha256)
    plan = clone(plan)
    if (type(plan) is not dict or digest(plan) != expected_plan_sha256
            or plan.get('schema') != 'keel.operational.qualification-plan.v1'
            or plan.get('execution_authorized') is not False
            or plan.get('submission_authorized') is not False):
        raise ValueError('synthetic_qualification_plan_pin_required')
    fixture = plan['fixture']
    if (fixture.get('synthetic') is not True or fixture['host_snapshot'].get('synthetic') is not True
            or digest(fixture) != plan['fixture_sha256']):
        raise ValueError('exact_synthetic_fixture_required')
    contract = validate_contract(fixture['contract'])
    grant = ActionGateway(contract, fixture['host_snapshot'], fixture['approvals']).issue(
        fixture['values'], now=fixture['now'], ttl=300)
    _validate_attachments(fixture['attachments'], grant['actions'])
    nonce = expected_plan_sha256[:32]
    page = fixture_html(contract, nonce).decode('utf-8')
    scope = [('Form', contract['form_id']), ('Account', contract['account_id']),
             ('Expected origin', contract['origin']), ('Contract SHA-256', digest(contract)),
             ('Qualification plan SHA-256', expected_plan_sha256), ('Fixture nonce', nonce)]
    visible = '<section aria-label="Synthetic fixture scope">' + ''.join(
        '<p>' + html.escape(label + ': ' + value) + '</p>' for label, value in scope)
    visible += '<p id="actual-origin">Observed browser origin: UNKNOWN</p></section>'
    page = page.replace('<form id="fixture-form"', visible + '<form id="fixture-form"', 1)
    # Add native accessible names without introducing controls or changing field IDs.
    for field in contract['fields']:
        marker = 'id="' + field['id'] + '" name="' + field['id'] + '"'
        page = page.replace(marker, marker + ' aria-label="' + html.escape(field['label'], quote=True) + '"')
    page = page.replace('</body>', '<script>' + _READOUT + '</script></body>', 1).encode('utf-8')
    payloads = {'fixture.html': page}
    attachments = {}
    for action in grant['actions']:
        if action['operation'] != 'attach_file':
            continue
        field = action['field_id']
        # Preserve the approved basename; native file metadata includes it.
        name = action['value']['name']
        safe_name(name)
        if '/' in name or name in payloads or name == 'fixture-export.json':
            raise ValueError('unique_flat_attachment_basename_required')
        payloads[name] = base64.b64decode(fixture['attachments'][field], validate=True)
        attachments[field] = {'path': name, 'approved_metadata': action['value']}
    home = new_home(home)
    for name, data in payloads.items():
        _write(home, name, data)
    report = {'schema': 'keel.operational.fixture-export.v1', 'synthetic': True,
              'plan_sha256': expected_plan_sha256, 'contract_sha256': digest(contract),
              'expected_origin': contract['origin'], 'attachments': attachments,
              'files': {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
              'served': False, 'browser_opened': False, 'model_calls': 0,
              'submission_authorized': False, **FLAGS}
    atomic_json(home / 'fixture-export.json', report)
    return report
