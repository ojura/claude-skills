-- Read annotation objects with TeX Live's PDF reader, including object streams.
-- URI bytes are hex encoded on stdout; PDF strings have already been decoded.
local document = assert(pdfe.open(arg[1]), "Cannot open compiled PDF")
assert(pdfe.getstatus(document) >= 0, "Cannot read encrypted or damaged PDF")
local pages = assert(pdfe.getnofpages(document), "Cannot read PDF page count")
assert(pages > 0, "PDF contains no readable pages")
io.write("Pages: ", tostring(pages), "\n")
for number = 1, pages do
    local page = assert(pdfe.getpage(document, number), "Cannot read PDF page")
    local annotations = pdfe.getarray(page, "Annots")
    if annotations then
        for index = 0, #annotations - 1 do
            local annotation = pdfe.getdictionary(annotations, index)
            if annotation and pdfe.getname(annotation, "Subtype") == "Link" then
                local action = pdfe.getdictionary(annotation, "A")
                if action and pdfe.getname(action, "S") == "URI" then
                    local uri = pdfe.getstring(action, "URI", true)
                    assert(uri, "URI action requires a PDF string")
                    local encoded = uri:gsub(".", function(c) return string.format("%02x", string.byte(c)) end)
                    io.write("URI: ", tostring(number), " ", encoded, "\n")
                end
            end
        end
    end
end
pdfe.close(document)
