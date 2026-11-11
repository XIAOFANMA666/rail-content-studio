import json
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
dsl = yaml.safe_load((ROOT / 'team_video_review.yml').read_text(encoding='utf-8'))
node = next(n for n in dsl['workflow']['graph']['nodes'] if n['id'] == 'report')
namespace = {}
exec(node['data']['code'], namespace)
main, rubric = namespace['main'], namespace['RUBRIC']


def evaluate(model=None, manual=None, image_status=200):
    metrics = {'frames':[{'time_sec':1.0},{'time_sec':4.0}], 'transcript':{'status':'ok','segments':[{'text':'train to London'}]}}
    result = main(json.dumps(metrics), 200, 'train to London', json.dumps(model or {}), image_status, json.dumps(manual or {}))
    assert set(result) == {'report'}
    return result['report']


class WorkflowRulesTest(unittest.TestCase):
    def test_21_items_and_unknown_are_not_zero(self):
        out = evaluate()
        self.assertEqual(len(rubric), 7)
        self.assertEqual(sum(len(items) for _,items in rubric),21)
        self.assertEqual(sum(item[2] for _,items in rubric for item in items),100)
        self.assertIn('总分：待确认',out)
        self.assertIn('判定：待人工核验',out)
        for _,items in rubric:
            for item in items: self.assertIn(item[0]+' '+item[1],out)

    def test_output_has_two_sections_without_suggestions(self):
        out=evaluate()
        headers=[line for line in out.splitlines() if line.startswith('## ')]
        self.assertEqual(headers,['## 评分','## 安全、转人工'])
        end = next(n for n in dsl['workflow']['graph']['nodes'] if n['id']=='end')
        self.assertEqual(end['data']['outputs'],[{'value_selector':['report','report'],'value_type':'string','variable':'report'}])

    def test_suggestions_are_optional_and_separate(self):
        model={'criteria':[{'id':'content_value','score':8,'evidence':[{'time_sec':1,'observation':'火车'}], 'suggestion':'增加路线信息'}]}
        out=evaluate(model)
        self.assertEqual([x for x in out.splitlines() if x.startswith('## ')],['## 评分','## 安全、转人工','## 优化建议'])
        self.assertIn('- 内容有价值：增加路线信息',out)
        model['criteria'][0]['evidence'][0]['time_sec']=999
        self.assertNotIn('## 优化建议',evaluate(model))

    def test_complete_manual_scoring_and_hard_fail(self):
        scores={item[0]:item[2] for _,items in rubric for item in items}
        for risk in ('face','flag','competitor_logo'):
            checks={'face':'absent','flag':'absent','competitor_logo':'absent'}
            checks[risk]='found'
            out=evaluate(manual={'scores':scores,'risk':checks})
            self.assertIn('总分：100 / 100',out)
            self.assertIn('判定：不合格',out)
        out=evaluate(manual={'scores':scores,'risk':{'face':'absent','flag':'absent','competitor_logo':'absent'}})
        self.assertIn('三项否决规则已人工确认未出现',out)

    def test_ai_score_requires_valid_evidence_and_does_not_score_human_only(self):
        model={'criteria':[{'id':'content_value','score':8,'evidence':[{'time_sec':1,'observation':'火车'}]}, {'id':'sound_clear','score':4,'evidence':[{'time_sec':1,'observation':'画面'}]}, {'id':'visual_appeal','score':4,'evidence':[{'time_sec':999,'observation':'不存在的帧'}]}]}
        out=evaluate(model)
        self.assertIn('content_value 内容有价值：8 / 8（AI 初评）',out)
        self.assertIn('sound_clear 配音清晰：待确认',out)
        self.assertIn('visual_appeal 画面具有吸引力：待确认',out)
        self.assertIn('总分：待确认',out)

    def test_suspected_is_handoff_not_automatic_pass(self):
        model={'risks':[{'id':'face','status':'suspected','time_sec':1,'observation':'可能有人脸'}]}
        out=evaluate(model)
        self.assertIn('人脸：人工待确认；AI疑似出现',out)
        self.assertIn('判定：待人工核验',out)
        self.assertIn('判定：不合格',evaluate(model,{'risk':{'face':'found'}}))
        model['risks'][0]['time_sec']=999
        self.assertIn('人脸：人工待确认；AI待确认',evaluate(model))

    def test_bad_manual_input_never_fabricates_results(self):
        out=evaluate(manual={'scores':{'opening_hook':True}})
        self.assertIn('人工分数无效',out)
        self.assertIn('待人工核验',out)
        self.assertEqual([x for x in out.splitlines() if x.startswith('## ')],['## 评分','## 安全、转人工'])
        self.assertIn('风险核验',evaluate(manual={'risk':{'face':'pass'}}))

    def test_image_error_cannot_create_ai_score(self):
        model={'criteria':[{'id':'content_value','score':8,'evidence':[{'time_sec':1,'observation':'火车'}]}]}
        self.assertIn('content_value 内容有价值：待确认',evaluate(model,image_status=500))

    def test_dsl_contains_no_embedded_environment_secrets(self):
        self.assertFalse(dsl['workflow'].get('environment_variables'))
        self.assertEqual(node['data']['code'],(ROOT/'report_code.py').read_text(encoding='utf-8'))
        for n in dsl['workflow']['graph']['nodes']:
            if n['data'].get('type') == 'http-request':
                self.assertEqual(n['data'].get('authorization',{}).get('type'),'no-auth')

if __name__=='__main__': unittest.main()
