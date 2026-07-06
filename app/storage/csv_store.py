import os
import logging
import pandas as pd

logger = logging.getLogger(__name__)


class CsvStore:
    def save(self, data: "list[dict] | pd.DataFrame", filepath: str, mode: str = "w") -> str:
        """Save data to CSV.

        Args:
            data: list of dicts or DataFrame
            filepath: destination path
            mode: "w" (overwrite) or "a" (append)

        Returns:
            Absolute path to the saved file
        """
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)

        if isinstance(data, list):
            df = pd.DataFrame(data)
        else:
            df = data.copy()

        if mode == "a" and os.path.exists(filepath):
            df.to_csv(filepath, mode="a", header=False, index=False, encoding="utf-8-sig")
        else:
            df.to_csv(filepath, mode="w", header=True, index=False, encoding="utf-8-sig")

        logger.debug("CsvStore.save: %s (%d rows, mode=%s)", filepath, len(df), mode)
        return os.path.abspath(filepath)

    def load(self, filepath: str) -> "pd.DataFrame | None":
        """Load CSV into DataFrame. Returns None if file not found."""
        if not os.path.exists(filepath):
            logger.debug("CsvStore.load: file not found: %s", filepath)
            return None
        try:
            df = pd.read_csv(filepath, encoding="utf-8-sig")
            logger.debug("CsvStore.load: %s (%d rows)", filepath, len(df))
            return df
        except Exception as exc:
            logger.warning("CsvStore.load failed for %s: %s", filepath, exc)
            return None

    def append_row(self, row: dict, filepath: str) -> None:
        """Append a single row dict to a CSV file."""
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)

        df = pd.DataFrame([row])
        write_header = not os.path.exists(filepath)
        df.to_csv(filepath, mode="a", header=write_header, index=False, encoding="utf-8-sig")
        logger.debug("CsvStore.append_row: %s <- %s", filepath, row)
