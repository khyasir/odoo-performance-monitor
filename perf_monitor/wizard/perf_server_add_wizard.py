# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..models.perf_common import COLLECTOR_CONF_PATH, COLLECTOR_RESTART_COMMAND
from ..models.perf_redact import is_allowed_central_url
from ..models.perf_server import name_key

CENTRAL_URL_PARAM = 'perf_monitor.central_url'


class PerfServerAddWizard(models.TransientModel):
    _name = 'perf.server.add.wizard'
    _description = 'Add Monitored Server'
    _transient_max_hours = 0.5

    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft', required=True)
    server_id = fields.Many2one('perf.server', readonly=True, ondelete='cascade')
    name = fields.Char(required=True, help='Unique name of the server card (the database name may repeat)')
    environment = fields.Selection([('staging', 'Staging'), ('live', 'Live')], string='Env', required=True,
                                   default='staging')
    host = fields.Char(required=True, help='Host name / IP of the Odoo server to monitor')
    db_name = fields.Char('Database', required=True, help='Monitored Odoo database name')
    pg_host = fields.Char('PostgreSQL host', default='localhost',
                          help='PostgreSQL / RDS endpoint as seen from the monitored server (optional root '
                               'collector only; perf_monitor_agent uses the Odoo database connection)')
    central_url = fields.Char('Central URL', required=True, default=lambda self: self._default_central_url(),
                              help='URL the collector uses to reach this Odoo. Must be https:// (plain http:// '
                                   'only for 127.0.0.1 / [::1] / localhost, i.e. a collector on this host). '
                                   'Saved as the system parameter perf_monitor.central_url, which install.sh '
                                   'uses (an http:// URL only when no valid URL is set yet).')
    central_db = fields.Char('Central database', readonly=True, default=lambda self: self.env.cr.dbname,
                             help='This database: paste it in Settings > Perf Monitor Agent > Central database '
                                  'of the monitored database.')
    install_command = fields.Text(readonly=True,
                                  help='Shown once. The token is not stored on the server record, only its hash.')
    # set by perf.server.action_regenerate_token: the done state shows the new token + collector.conf steps
    mode = fields.Selection([('add', 'Add server'), ('regenerate', 'Regenerate token')], default='add',
                            required=True, readonly=True)
    source_kind = fields.Selection([('collector', 'Collector'), ('agent', 'DB agent')], default='collector',
                                   readonly=True)
    token = fields.Char('Token', readonly=True, help='Shown once, wiped when the wizard is closed.')
    conf_path = fields.Char('Config file', readonly=True, default=COLLECTOR_CONF_PATH)
    conf_command = fields.Text('Update collector.conf', readonly=True,
                               help='Replaces the token line of collector.conf with the new token.')
    restart_command = fields.Char('Restart', readonly=True, default=COLLECTOR_RESTART_COMMAND)

    @api.model
    def _default_central_url(self):
        params = self.env['ir.config_parameter'].sudo()
        for key in (CENTRAL_URL_PARAM, 'web.base.url'):
            url = (params.get_param(key) or '').strip().rstrip('/')
            if is_allowed_central_url(url):
                return url
        return 'http://127.0.0.1:8069'

    @api.constrains('central_url')
    def _check_central_url(self):
        for wizard in self:
            if not is_allowed_central_url((wizard.central_url or '').strip().rstrip('/')):
                raise ValidationError(_(
                    'Central URL %s is not allowed: use https:// (plain http:// only for 127.0.0.1, [::1] or '
                    'localhost).', wizard.central_url))

    def _check_manager(self):
        if not self.env.user.has_group('perf_monitor.group_perf_manager'):
            raise UserError(_('Only Perf Monitor Managers can add servers.'))

    def _show_install_command(self, token):
        """Add Server: the plain token (copy button) + the perf_monitor_agent settings to paste, and the
        optional legacy root collector installer."""
        self.ensure_one()
        self.write({
            'state': 'done', 'token': token, 'central_db': self.env.cr.dbname,
            'central_url': (self.central_url or self._default_central_url()).strip().rstrip('/'),
            'install_command': self.env['perf.server']._build_install_command(
                self.central_url or self._default_central_url(), token, self.pg_host, self.db_name),
        })
        return {
            'type': 'ir.actions.act_window', 'name': _('Server token'),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'new',
        }

    def _show_new_token(self, token):
        """Regenerate: show the plain token once, with the collector.conf path, a one-liner writing it
        there and the restart command (the full installer is only an alternative)."""
        self.ensure_one()
        self.write({
            'state': 'done', 'mode': 'regenerate', 'token': token,
            'conf_path': COLLECTOR_CONF_PATH, 'restart_command': COLLECTOR_RESTART_COMMAND,
            # token_urlsafe: [A-Za-z0-9_-] only, safe inside the sed expression
            'conf_command': "sudo sed -i 's|^token *=.*|token = %s|' %s && %s" % (
                token, COLLECTOR_CONF_PATH, COLLECTOR_RESTART_COMMAND),
            'install_command': self.env['perf.server']._build_install_command(
                self.central_url or self._default_central_url(), token, self.pg_host, self.db_name),
        })
        return {
            'type': 'ir.actions.act_window', 'name': _('New collector token'),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'new',
        }

    def _store_central_url(self, url):
        """install.sh renders CENTRAL_URL from this single global parameter (never from the request's
        Host header). An https URL always replaces it; a loopback http URL (collector on this host)
        is stored only while no valid URL is set, so it cannot break install.sh for remote servers."""
        params = self.env['ir.config_parameter'].sudo()
        current = (params.get_param(CENTRAL_URL_PARAM) or '').strip().rstrip('/')
        if url.startswith('https://') or not is_allowed_central_url(current):
            params.set_param(CENTRAL_URL_PARAM, url)

    @api.constrains('name', 'state')
    def _check_name_free(self):
        for wizard in self.filtered(lambda w: w.state == 'draft' and w.mode == 'add' and w.name):
            if any(name_key(o.name) == name_key(wizard.name)
                   for o in self.env['perf.server'].with_context(active_test=False).search([])):
                raise ValidationError(_('A server named "%s" already exists: server names must be unique (the '
                                        'database name may repeat).', wizard.name))

    def action_confirm(self):
        self.ensure_one()
        self._check_manager()
        server = self.env['perf.server'].create({
            'name': self.name, 'environment': self.environment, 'host': self.host, 'db_name': self.db_name,
        })
        self.server_id = server
        self._store_central_url(self.central_url.strip().rstrip('/'))
        token = server._set_new_token()
        server.message_post(body=_('Server added and collector token issued by %s.', self.env.user.name))
        return self._show_install_command(token)

    def action_close(self):
        # wipe the one-time token / commands (they contain the plain token) as soon as they are copied
        self.write({'install_command': False, 'conf_command': False, 'token': False})
        if self.server_id:
            return {
                'type': 'ir.actions.act_window', 'res_model': 'perf.server', 'res_id': self.server_id.id,
                'view_mode': 'form', 'target': 'current',
            }
        return {'type': 'ir.actions.act_window_close'}
