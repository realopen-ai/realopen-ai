Convert the user request into ONE JSON workbook specification.
Output ONLY the JSON object — no prose, no markdown fences.

Schema:
{
  "filename": "snake_case_name.xlsx",
  "sheets": [
    {
      "name": "Sheet1",
      "tables": [
        {
          "start_cell": "A1",
          "headers": ["Month", "Sales"],
          "rows": [["Jan", 100], ["Feb", 200]],
          "total_row": ["Total", "=SUM(B2:B13)"],
          "title": "Sales Data"
        }
      ],
      "data_validation": [
        {"range": "B4:B31", "values": ["Breakfast", "Lunch", "Dinner", "Snack"]}
      ],
      "conditional_formats": [
        {"range": "B4:B31", "rules": [{"type": "cell_is", "operator": "greater_than", "value": 0, "fill": "C6EFCE", "font_color": "1E4620"}]}
      ]
    }
  ]
}

Rules:
- "sheets" is a non-empty array; every sheet has a "name" and at least one table.
- "rows" are arrays of values; numbers MUST be JSON numbers, not strings.
- "total_row" is an array aligned with the headers; use =SUM() formulas.
- "start_cell" points at the FIRST HEADER cell (A1 when in doubt).
- "data_validation" (dropdown lists) MUST be an ARRAY of {"range", "values"} objects — NEVER an object keyed by column name. "range" covers the table's data rows, e.g. "C4:C31".
- "conditional_formats" MUST be an ARRAY of {"range", "rules"} objects; rules use {"type": "cell_is", "operator": "equal|greater_than|less_than", "value": …, "fill": "RRGGBB", "font_color": "RRGGBB"}.
- Keep it small: at most 15 rows of sample data is fine.

Respond with the JSON object only.
