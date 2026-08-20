import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app

class AdminAPITests(unittest.TestCase):
    def test_list_templates(self):
        client = TestClient(app)
        # Admin requests require admin auth headers
        response = client.get('/api/admin/v1/templates', headers={'Authorization': 'Bearer test-admin-key'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('openai', data)
        self.assertIn('anthropic', data)

    def test_create_provider_connection_and_revision(self):
        client = TestClient(app)

        # 1. Create a provider connection from template
        payload = {
            'template_id': 'openai',
            'name': 'My custom OpenAI connection',
            'base_url': 'https://api.openai.com/v1',
            'api_key': 'sk-some-mock-key'
        }
        response = client.post('/api/admin/v1/providers', json=payload, headers={'Authorization': 'Bearer test-admin-key'})
        self.assertEqual(response.status_code, 201)
        conn = response.json()
        self.assertEqual(conn['name'], 'My custom OpenAI connection')
        self.assertEqual(conn['base_url'], 'https://api.openai.com/v1')
        self.assertNotIn('api_key', conn)

        # 2. Get the active revision
        response = client.get('/api/admin/v1/revisions/active', headers={'Authorization': 'Bearer test-admin-key'})
        self.assertEqual(response.status_code, 200)
        active_rev = response.json()
        self.assertIsNotNone(active_rev)

        # 3. Create a revision draft
        payload_rev = {
            'routes': {
                'claude-router-main': {
                    'strategy': 'priority',
                    'candidates': [
                        {'upstream': 'openai', 'model': 'gpt-4o', 'weight': 1}
                    ]
                }
            }
        }
        response = client.post('/api/admin/v1/revisions', json=payload_rev, headers={'Authorization': 'Bearer test-admin-key'})
        self.assertEqual(response.status_code, 201)
        draft = response.json()
        draft_id = draft['revision_id']

        # 4. Activate the draft revision
        response = client.post(f'/api/admin/v1/revisions/{draft_id}/activate', headers={'Authorization': 'Bearer test-admin-key'})
        self.assertEqual(response.status_code, 200)

        # 5. Check active revision changed
        response = client.get('/api/admin/v1/revisions/active', headers={'Authorization': 'Bearer test-admin-key'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['revision_id'], draft_id)


if __name__ == '__main__':
    unittest.main()
