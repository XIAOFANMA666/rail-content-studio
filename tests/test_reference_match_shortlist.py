import unittest

from reference_match_shortlist import _validate_result


class ShortlistValidationTests(unittest.TestCase):
    def test_invented_or_duplicate_id_fails_open_without_losing_candidates(self):
        for rows in ([{'id':'invented','decision':'exclude'}],
                     [{'id':'m1','decision':'exclude','reason':'wrong subject','visual_evidence':'C01 contains a car','hard_mismatch':'subject'}]*2):
            with self.subTest(rows=rows):
                result=_validate_result({'candidates':rows},['m1','m2'],{'m1','m2'})
                self.assertFalse(result['model_valid'])
                self.assertEqual(result['selected_ids'],['m1','m2'])
                self.assertEqual(result['excluded_ids'],[])

    def test_only_evidenced_hard_mismatch_excluded_uncertainty_and_unseen_retained(self):
        response={'candidates':[
            {'id':'m1','decision':'exclude','hard_mismatch':'subject','visual_evidence':'C01 shows only a parked car in all frames','reason':'wrong subject'},
            {'id':'m2','decision':'uncertain','hard_mismatch':None,'visual_evidence':'subject partly covered','reason':'cannot establish action'},
            {'id':'m3','decision':'exclude','hard_mismatch':'shot_size','visual_evidence':'','reason':'uncertain'},
            {'id':'m4','decision':'exclude','hard_mismatch':'subject','visual_evidence':'invented observation','reason':'has no frames'},
        ]}
        result=_validate_result(response,['m1','m2','m3','m4','m5'],{'m1','m2','m3'})
        self.assertEqual(result['excluded_ids'],['m1'])
        self.assertEqual(result['selected_ids'],['m2','m3','m4','m5'])
        self.assertEqual(result['uncertain_ids'],['m2','m3','m4','m5'])


if __name__=='__main__':unittest.main()
