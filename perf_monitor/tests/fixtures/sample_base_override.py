# -*- coding: utf-8 -*-
# Test fixture for perf_monitor/tests (code context, lessons): never imported, only read as text.
from odoo import models

MODULE_LEVEL_CONSTANT = 1


class SampleBaseOverride(models.AbstractModel):
    """A generic write override: runs for every model."""
    _inherit = 'base'
    _description = 'Sample'

    def unrelated(self):
        return 1

    def write(self, vals):
        rules = self.env['sample.rule'].search([('model_id.model', '=', self._name)])
        for rule in rules:
            rule.check(self)
        return super().write(vals)

    def long_method(self):
        total = 0
        total += 1
        total += 2
        total += 3
        total += 4
        total += 5
        total += 6
        total += 7
        total += 8
        total += 9
        total += 10
        total += 11
        total += 12
        total += 13
        total += 14
        total += 15
        total += 16
        total += 17
        total += 18
        total += 19
        total += 20
        return total
