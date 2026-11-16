-- Use the PDF reader included in TeX Live when Poppler's pdfinfo is unavailable.
local document = assert(pdfe.open(arg[1]), "Cannot open compiled PDF")
local pages = pdfe.getnofpages(document)
pdfe.close(document)
assert(pages and pages > 0, "Compiled PDF contains no readable pages")
io.write("Pages: ", tostring(pages), "\n")
