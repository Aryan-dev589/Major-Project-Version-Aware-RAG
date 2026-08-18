import json
import unittest

from whatif_ai import stream_evaluate_scenario


class DummyLLM:
    def complete(self, messages):
        return '{"verdict":"compliant","confidence":80,"explanation":"safe","required_actions":["review"],"applicable_sections":["Policy - Section 1"]}'

    def stream_complete(self, messages):
        yield '{"verdict":"compliant","confidence":80,'
        yield '"explanation":"safe","required_actions":["review"],' 
        yield '"applicable_sections":["Policy - Section 1"]}'


class WhatIfStreamTest(unittest.TestCase):
    def test_stream_evaluate_scenario_emits_sse_events(self):
        import whatif_ai

        def fake_retrieval(scenario, user_role, user_department, top_k_retrieve, top_k_rerank):
            return {
                'citations': [],
                'top_chunks': [{
                    'policy_name': 'Policy',
                    'version': '1.0',
                    'section': 'Section 1',
                    'text': 'Employees may work remotely.'
                }],
                'user_prompt': 'scenario',
            }

        whatif_ai._prepare_scenario_context = fake_retrieval
        whatif_ai.get_llm = lambda: DummyLLM()

        events = list(stream_evaluate_scenario('Can I work remote?'))
        self.assertTrue(any('data:' in event for event in events))
        self.assertTrue(any('done' in event for event in events))


if __name__ == '__main__':
    unittest.main()
