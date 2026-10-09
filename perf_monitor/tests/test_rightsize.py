# -*- coding: utf-8 -*-
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models import perf_rightsize as rs


def _series(days, value, per_day=24):
    end = datetime(2026, 9, 27, tzinfo=timezone.utc)
    return [(end - timedelta(hours=h + 1), value) for h in range(days * per_day)]


class FakeCloudWatch:
    def __init__(self, series):
        self.series = series
        self.calls = []

    def get_metric_data(self, **kw):
        stat = kw['MetricDataQueries'][0]['MetricStat']
        name = (stat['Metric']['Namespace'], stat['Metric']['MetricName'])
        self.calls.append(name)
        points = self.series.get(name, [])
        if 'NextToken' not in kw and len(points) > 100:  # exercise pagination
            half = len(points) // 2
            return {'MetricDataResults': [{'Timestamps': [p[0] for p in points[:half]],
                                           'Values': [p[1] for p in points[:half]]}], 'NextToken': 't'}
        points = points[len(points) // 2:] if 'NextToken' in kw else points
        return {'MetricDataResults': [{'Timestamps': [p[0] for p in points], 'Values': [p[1] for p in points]}]}


class FakeEc2:
    def describe_instances(self, InstanceIds):
        return {'Reservations': [{'Instances': [{'InstanceType': 't2.large'}]}]}

    def describe_instance_types(self, InstanceTypes):
        specs = {'t2.medium': (2, 4096), 't2.large': (2, 8192), 't2.xlarge': (4, 16384)}
        return {'InstanceTypes': [{'InstanceType': t, 'VCpuInfo': {'DefaultVCpus': specs[t][0]},
                                   'MemoryInfo': {'SizeInMiB': specs[t][1]}} for t in InstanceTypes]}


class FakeRds:
    def describe_db_instances(self, DBInstanceIdentifier):
        return {'DBInstances': [{'DBInstanceClass': 'db.t4g.medium', 'MultiAZ': False}]}

    def get_paginator(self, name):
        raise AssertionError('rds_instance_id is set: no detection')


class FakePricing:
    def get_products(self, **kw):
        raise RuntimeError('AccessDenied')


@tagged('post_install', '-at_install')
class TestRightsizeRules(TransactionCase):
    def test_p95_days_neighbour(self):
        self.assertIsNone(rs.p95([]))
        self.assertAlmostEqual(rs.p95(list(range(101))), 95.0)
        self.assertEqual(rs.p95([5.0]), 5.0)
        self.assertEqual(rs.distinct_days([p[0] for p in _series(3, 1)]), 3)
        self.assertEqual(rs.neighbour('t2.large', -1), 't2.medium')
        self.assertEqual(rs.neighbour('db.t4g.medium', +1), 'db.t4g.large')
        self.assertIsNone(rs.neighbour('t2.nano', -1))
        self.assertIsNone(rs.neighbour('weird', 1))
        self.assertEqual(rs.rds_max_connections(4), 450)

    def test_recommend(self):
        specs, prices = rs.EC2_SPECS, rs.EC2_PRICES
        low = {'p95': 10.0, 'days': 14}
        reco = rs.recommend('ec2', 't2.large', specs, prices, {'p95': 20.0, 'days': 5}, low)
        self.assertEqual(reco['action'], 'insufficient')
        self.assertEqual(reco['reason'], 'insufficient data: 5/14 days (CPU)')
        reco = rs.recommend('ec2', 't2.large', specs, prices, {'p95': 20.0, 'days': 14}, {'p95': 20.0, 'days': 3})
        self.assertEqual(reco['action'], 'keep')
        self.assertIn('RAM: insufficient data: 3/14 days', reco['reason'])
        reco = rs.recommend('ec2', 't2.large', specs, prices, {'p95': 20.0, 'days': 14}, {'p95': 20.0, 'days': 14})
        self.assertEqual((reco['action'], reco['target']), ('downsize', 't2.medium'))
        self.assertAlmostEqual(reco['saving_month'], round((0.1008 - 0.05) * 730, 2))
        reco = rs.recommend('ec2', 't2.large', specs, prices, {'p95': 20.0, 'days': 14}, {'p95': 50.0, 'days': 14})
        self.assertEqual(reco['action'], 'keep')   # RAM would be 100 % on t2.medium
        self.assertIn('RAM would be 100%', reco['reason'])
        reco = rs.recommend('ec2', 't2.large', specs, prices, {'p95': 85.0, 'days': 14}, low)
        self.assertEqual((reco['action'], reco['target']), ('upsize', 't2.xlarge'))
        self.assertLess(reco['saving_month'], 0)
        rds = rs.recommend('rds', 'db.t4g.medium', rs.RDS_SPECS, rs.RDS_PRICES, {'p95': 20.0, 'days': 14},
                           {'p95': 30.0, 'days': 14}, {'p95': 400.0, 'days': 14})
        self.assertEqual(rds['action'], 'upsize')   # 400 of 450 connections
        rds = rs.recommend('rds', 'db.t4g.medium', rs.RDS_SPECS, rs.RDS_PRICES, {'p95': 20.0, 'days': 14},
                           {'p95': 30.0, 'days': 14}, {'p95': 20.0, 'days': 14})
        self.assertEqual((rds['action'], rds['target']), ('downsize', 'db.t4g.small'))
        line = rs.card_line('RDS', 'db.t4g.medium', rds, {'p95': 20.0, 'days': 14}, {'p95': 30.0, 'days': 14},
                            {'p95': 20.0})
        self.assertIn('downsize → db.t4g.small, save $', line)


@tagged('post_install', '-at_install')
class TestRightsizeCard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = cls.env['perf.server'].create({
            'name': 'staging', 'host': '10.0.0.1', 'db_name': 'x', 'instance_id': 'i-0abc',
            'instance_type': 't2.large', 'rds_instance_id': 'demo-rds'})

    def _run(self, series, ec2_days=14):
        cw = FakeCloudWatch(series)
        clients = {'cloudwatch': cw, 'ec2': FakeEc2(), 'rds': FakeRds(), 'pricing': FakePricing()}
        with patch.object(type(self.server), '_aws_client', lambda self, service, region: clients[service]):
            self.server.action_compute_rightsize()
        return cw

    def test_card_with_cloudwatch(self):
        gib4 = 4 * 1024 ** 3
        series = {
            ('AWS/EC2', 'CPUUtilization'): _series(14, 12.0),
            ('AWS/RDS', 'CPUUtilization'): _series(14, 15.0),
            ('AWS/RDS', 'FreeableMemory'): _series(14, gib4 * 0.8),   # 20 % used
            ('AWS/RDS', 'DatabaseConnections'): _series(14, 30.0),
        }
        cw = self._run(series)
        self.assertIn(('CWAgent', 'mem_used_percent'), cw.calls)
        data = json.loads(self.server.rs_data)
        self.assertEqual(data['ec2']['type'], 't2.large')
        self.assertAlmostEqual(data['ec2']['cpu']['p95'], 12.0)
        self.assertEqual(data['ec2']['mem']['source'], 'collector')   # no CWAgent metric
        self.assertEqual(data['ec2']['reco']['action'], 'keep')
        self.assertIn('RAM insufficient data: 0/14 days', self.server.rs_ec2_line)
        self.assertEqual(data['rds']['reco']['action'], 'downsize')
        self.assertAlmostEqual(data['rds']['mem']['p95'], 20.0, places=3)
        self.assertEqual(data['price_source'], 'static eu-west-1 table')
        self.assertTrue(self.server.rs_rds_line.startswith('RDS db.t4g.medium: downsize → db.t4g.small'))
        self.assertAlmostEqual(self.server.rs_saving_month, round((0.069 - 0.035) * 730, 2))
        self.assertIn('EC2 t2.large', self.server.rightsize_recommendation)
        self.assertIn('<table', self.server.rs_html)

    def test_insufficient_data(self):
        self._run({('AWS/EC2', 'CPUUtilization'): _series(3, 12.0), ('AWS/RDS', 'CPUUtilization'): _series(3, 1.0)})
        self.assertEqual(self.server.rs_ec2_line, 'EC2 t2.large: insufficient data: 3/14 days (CPU)')
        self.assertEqual(self.server.rs_saving_month, 0.0)

    def test_aws_failure_is_shown_not_raised(self):
        def boom(self, service, region):
            raise RuntimeError('Unable to locate credentials')
        with patch.object(type(self.server), '_aws_client', boom):
            self.server.action_compute_rightsize()
        self.assertIn('CloudWatch unavailable', self.server.rs_rds_line)
        self.assertIn('insufficient data', self.server.rightsize_recommendation)   # collector-only text kept

    def test_status_cron_does_not_call_aws(self):
        with patch.object(type(self.server), '_aws_client', side_effect=AssertionError('no AWS every 5 min')):
            self.env['perf.server']._cron_refresh()
        self.assertTrue(self.server.rightsize_recommendation)
