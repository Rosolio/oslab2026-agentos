"""Create a text PDF for offline demonstrations."""

from pathlib import Path

import pymupdf

path = Path("var/materials/operating-systems.pdf")
path.parent.mkdir(parents=True, exist_ok=True)
with pymupdf.open() as pdf:
    page = pdf.new_page()
    page.insert_text((72, 72), "Operating Systems: Scheduling and Recovery", fontsize=16)
    page.insert_text(
        (72, 110),
        "FIFO executes ready tasks in arrival order.\n"
        "A blocked task releases the execution slot.\n"
        "A checkpoint saves durable progress and intermediate results.\n"
        "After a crash, execution resumes from the last committed checkpoint.",
    )
    pdf.save(path)
print(path)
