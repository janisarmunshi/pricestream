from rest_framework.renderers import BaseRenderer

from apps.api.toon import encode_tabular


class TOONRenderer(BaseRenderer):
    """?format=toon (or Accept: application/toon) on any paginated tick/instrument
    list response. Renders the paginated envelope's `results` array as one TOON
    tabular block; `next`/`previous` cursor links are carried as TOON comments
    above it (TOON has no metadata slot alongside a single top-level array) so a
    consumer paging through can still follow the pagination without a second
    JSON response to cross-reference.
    """
    media_type = 'application/toon'
    format = 'toon'
    charset = 'utf-8'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict) and 'results' in data:
            results = data['results']
            header_lines = []
            if data.get('next'):
                header_lines.append(f"# next: {data['next']}")
            if data.get('previous'):
                header_lines.append(f"# previous: {data['previous']}")
        else:
            results = data if isinstance(data, list) else [data]
            header_lines = []

        if not results:
            body = 'results[0]{}:\n'
        else:
            fields = list(results[0].keys())
            body = encode_tabular('results', results, fields)

        return ('\n'.join(header_lines) + ('\n' if header_lines else '') + body).encode('utf-8')
