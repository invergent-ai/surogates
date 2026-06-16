import { describe, expect, it, vi } from 'vitest';

import { resolvePairing } from '../src/protocol';

describe('resolvePairing', () => {
  it('resolves agent_id + api_web_url from the pairing endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ agent_id: 'a1', api_web_url: 'https://acme.com' }),
    });

    const out = await resolvePairing(
      'https://api.surogate.ai',
      'surg_wk_k',
      fetchMock as unknown as typeof fetch,
    );

    expect(out).toEqual({ agentId: 'a1', apiWebUrl: 'https://acme.com' });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.surogate.ai/api/widget/p/surg_wk_k',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('trims a trailing slash on the pairing base', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ agent_id: 'a1', api_web_url: 'https://acme.com' }),
    });

    await resolvePairing(
      'https://api.surogate.ai/',
      'surg_wk_k',
      fetchMock as unknown as typeof fetch,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.surogate.ai/api/widget/p/surg_wk_k',
      expect.anything(),
    );
  });

  it('throws on an unknown key (non-ok response)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    });

    await expect(
      resolvePairing(
        'https://api.surogate.ai',
        'surg_wk_bad',
        fetchMock as unknown as typeof fetch,
      ),
    ).rejects.toThrow();
  });
});
