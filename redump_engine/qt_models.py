"""Qt sorting roles keep aggregate values numeric and disc children stable."""
from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt

SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
INDICES_ROLE = SORT_ROLE + 1
DISC_ROLE = SORT_ROLE + 2
PATH_ROLE = SORT_ROLE + 3


class LibrarySortModel(QSortFilterProxyModel):
    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:
        if left.parent().isValid() and right.parent() == left.parent():
            first = left.sibling(left.row(), 0).data(DISC_ROLE) or 0
            second = right.sibling(right.row(), 0).data(DISC_ROLE) or 0
            return first < second if self.sortOrder() == Qt.SortOrder.AscendingOrder else first > second
        first, second = left.data(SORT_ROLE), right.data(SORT_ROLE)
        if first is None or second is None:
            if first is None and second is None:
                return str(left.data() or "").casefold() < str(right.data() or "").casefold()
            return first is not None if self.sortOrder() == Qt.SortOrder.AscendingOrder else first is None
        if isinstance(first, str):
            first, second = first.casefold(), str(second).casefold()
        return first < second
