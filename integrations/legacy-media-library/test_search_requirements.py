"""Hard search conditions remain intact across retrieval and agent planning."""
import sqlite3
import unittest

import commons
import shot_search


class SearchRequirementTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE asset(id TEXT,name TEXT,extension TEXT,info TEXT,status TEXT);
            CREATE TABLE segment(id INTEGER,asset TEXT,payload TEXT,state TEXT);
        ''')

    def test_all_conditions_and_long_explicit_place_reach_search_and_agent(self):
        location = ('Scotland station '+('specific platform detail ' * 8)).strip()
        conditions = ['condition '+str(index) for index in range(12)] + ['long feature '+('x'*120)]
        script = 'A woman walks with luggage\nThen a train passes the platform.'
        body = dict(script=script,required='\n'.join(conditions),location=location,
                    line='long named route '+('r'*120),keywords='woman station',infer=False,videoOnly=True)
        gap = shot_search.search(self.conn,body)
        self.assertTrue(set(conditions).issubset(gap['rules']))
        self.assertIn(location,gap['rules'])
        self.assertIn(body['line'],gap['rules'])
        self.assertNotIn('苏格兰',gap['rules'])
        payload = commons.job_payload(body,gap)
        self.assertEqual(payload['script'],script)
        self.assertEqual(payload['search']['required'],body['required'])
        self.assertEqual(payload['search']['location'],location)
        self.assertEqual(payload['rules'],gap['rules'])
        info = dict(title='unrelated train',description='',mime='video/mp4',license='',author='',
                    licenseUrl='',width=0,height=0)
        _,gaps,_ = commons.assess(info,payload)
        self.assertIn('缺少地点线索：'+location,gaps)
        self.assertIn('缺少线路线索：'+body['line'],gaps)

    def test_oversized_conditions_rejected_and_whole_aliases_still_work(self):
        for field,limit in (('script',1000),('location',500),('line',500),('required',4000)):
            body = {'script':'train',field:'x'*(limit+1)}
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,str(limit)):
                shot_search.search(self.conn,body)
        self.assertEqual(shot_search.explicit_names('Scotland',shot_search.PLACES),{'苏格兰'})


if __name__ == '__main__':
    unittest.main()
