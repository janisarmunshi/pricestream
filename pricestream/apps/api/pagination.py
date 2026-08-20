from rest_framework.pagination import CursorPagination


class TickCursorPagination(CursorPagination):
    """Cursor-based, not offset-based — offset pagination degrades badly on large
    time-series tables when a single query can span millions of rows.
    """
    page_size = 500
    max_page_size = 5000
    ordering = 'time'
