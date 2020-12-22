# functions to guess the gene model release given a list of gene IDs
# tested on python3 and python2
import logging, sys, optparse, string, glob, gzip, json
from io import StringIO
#from urllib.request import urlopen
from urllib.request import Request, urlopen
from collections import defaultdict
from os.path import join, basename, dirname, isfile

from .cellbrowser import sepForFile, getStaticFile, openFile, splitOnce, setDebug, getStaticPath
from .cellbrowser import getGeneSymPath, downloadUrlLines, getSymToGene, getGeneBedPath, errAbort, iterItems
from .cellbrowser import findCbData, readGeneSymbols, getGeneJsonPath

# ==== functions =====
def cbGenes_parseArgs():
    " setup logging, parse command line arguments and options. -h shows auto-generated help page "
    parser = optparse.OptionParser("""usage: %prog [options] command - download gene model files and auto-detect the version.

    Commands:
    avail - List all gene models that can be downloaded
    syms <geneType> - Download a table with geneId <-> symbol for a gene model database.
    locs <assembly> <geneType> - Download a gene model file from UCSC, pick one transcript per gene and save to ~/cellbrowserData/genes/<db>.<geneType>.bed.
    fetch <assembly> <geneType> - do both 'syms' and 'locs'
    ls here - list all available gene models on this machine
    ls remote - list all available gene models at UCSC
    guess <inFile> <organism> - Guess best gene type. Reads the first tab-sep field from inFile and prints genetypes sorted by % of matching unique IDs to inFile.

    Examples:
    %prog avail
    %prog syms gencode-34 # for human gencode release 34
    %prog syms gencode-M25 # for mouse gencode release M25
    %prog locs hg38 gencode-34
    %prog locs mm10 gencode-M25
    %prog ls
    %prog guess genes.txt mouse
    %prog index # only used at UCSC to prepare the files for 'guess'
    """)

    parser.add_option("-d", "--debug", dest="debug", action="store_true", help="show debug messages")
    (options, args) = parser.parse_args()

    if args==[]:
        parser.print_help()
        exit(1)

    setDebug(options.debug)
    return args, options

# ----------- main --------------
def parseSignatures(org, geneIdType):
    " return dict with gene release -> list of unique signature genes "
    ret = {}
    logging.info("Parsing gencode release signature genes")
    fname = getStaticFile("genes/%s.%s.unique.tsv.gz" % (org, geneIdType))
    logging.info("Parsing %s" % fname)
    genes = set()
    verToGenes = {}
    for line in openFile(fname):
        if line.startswith("#"):
            continue
        version, geneIds = line.rstrip("\n").split('\t')
        geneIds = geneIds.split("|")
        verToGenes[version] = geneIds
    return verToGenes
        
def guessGeneIdType(genes):
    " return tuple organism / identifier type "
    gene1 = list(genes)[0]
    if gene1.startswith("ENSG"):
        return "human", "ids"
    if gene1.startswith("ENSMUS"):
        return "mouse", "ids"
    if gene1.upper()==gene1:
        return "human", "syms"
    else:
        return "mouse", "syms"

def parseGenes(fname):
    " return gene IDs in column 1 of file "
    fileGenes = set()
    headDone = False
    logging.info("Parsing first column from %s" % fname)
    sep = sepForFile(fname)
    for line in openFile(fname):
        if not headDone:
            headDone = True
            continue
        geneId = splitOnce(line[:50], sep)[0]
        geneId = geneId.strip("\n").strip("\r").strip()
        fileGenes.add(geneId.split('.')[0].split("|")[0])
    logging.info("Read %d genes" % len(fileGenes))
    return fileGenes

def guessGencodeVersion(fileGenes, signGenes):
    logging.info("Number of genes that are specific for gene model release:")
    diffs = []
    for version, uniqGenes in signGenes.items():
        intersection = list(fileGenes.intersection(uniqGenes))
        infoStr = "release "+version+": %d out of %d" % (len(intersection), len(uniqGenes))
        if len(intersection)!=0:
            expStr = ", ".join(intersection[:5])
            infoStr += (" e.g. "+ expStr)
        logging.info(infoStr)
        diffs.append((len(intersection), version))

    diffs.sort(reverse=True)
    bestVersion = diffs[0][1]
    return bestVersion

def guessGencode(fname, org):
    inGenes = set(parseGenes(fname))
    guessOrg, geneType = guessGeneIdType(inGenes)
    if org is None:
        org = guessOrg
    logging.info("Looks like input gene list is from organism %s, IDs are %s" % (org, geneType))
    signGenes = parseSignatures(org, geneType)
    bestVersion = guessGencodeVersion(inGenes, signGenes)
    print("Best %s Gencode release\t%s" % (org, bestVersion))

def buildSymbolTable(geneType):
    if geneType.startswith("gencode"):
        release = geneType.split("-")[1]
        rows = iterGencodePairs(release)
    else:
        errAbort("unrecognized gene type '%s'" % geneType)

    outFname = getStaticPath(getGeneSymPath(geneType))
    writeRows(rows, outFname)

def iterGencodePairs(release, doTransGene=False):
    " generator, yields geneId,symbol or transId,geneId pairs for a given gencode release"
    # e.g. trackName = "wgEncodeGencodeBasicV34"
    #attrFname = trackName.replace("Basic", "Attrs").replace("Comp", "Attrs")
    #assert(release[1:].isdigit())
    db = "hg38"
    if release[0]=="M":
        db = "mm10"
    if release in ["7", "14", "17", "19"] or "lift" in release:
        db = "hg19"
    url = "https://hgdownload.cse.ucsc.edu/goldenPath/%s/database/wgEncodeGencodeAttrsV%s.txt.gz" %  (db, release)
    logging.info("Downloading %s" % url)
    doneIds = set()
    for line in downloadUrlLines(url):
        row = line.rstrip("\n").split("\t")

        if doTransGene:
            # key = transcript ID, val is geneId
            key = row[4]
            val = row[0]
            val = val
        else:
            # key = geneId, val is symbol
            key = row[0]
            key = key
            val = row[1]

        if key not in doneIds:
            yield key, val
            doneIds.add(key)

def iterGencodeBed(db, release):
    " generator, yields a BED12+1 with a 'canonical' transcript for every gencode comprehensive gene "
    transToGene = dict(iterGencodePairs(release, doTransGene=True))

    url = "http://hgdownload.cse.ucsc.edu/goldenPath/%s/database/wgEncodeGencodeCompV%s.txt.gz" % (db, release)
    logging.info("Downloading %s" % url)
    geneToTransList = defaultdict(list)
    for line in downloadUrlLines(url):
        row = tuple(line.split('\t'))
        transId = row[1]
        geneId = transToGene[transId]
        score = int(''.join(c for c in geneId if c.isdigit())) # extract only the xxx part of the ENSGxxx ID
        geneToTransList[geneId].append( (score, row) )

    logging.info("Picking one transcript per gene")
    for geneId, transList in iterItems(geneToTransList):
        transList.sort() # prefer older transcripts
        canonTransRow = transList[0][1]
        binIdx, name, chrom, strand, txStart, txEnd, cdsStart, cdsEnd, exonCount, exonStarts, exonEnds, score, name2, cdsStartStat, cdsEndStat, exonFrames = canonTransRow
        blockStarts = []
        blockLens = []
        for exonStart, exonEnd in zip(exonStarts.split(","), exonEnds.split(",")):
            if exonStart=="":
                continue
            blockSize = int(exonEnd)-int(exonStart)
            blockStarts.append(exonStart)
            blockLens.append(str(blockSize))
        newRow = [chrom, txStart, txEnd, geneId, score, strand, cdsStart, cdsEnd, exonCount, ",".join(blockLens), ",".join(blockStarts), name2]
        yield newRow

def writeRows(rows, outFname):
    with openFile(outFname, "wt") as ofh:
        for row in rows:
            ofh.write("\t".join(row))
            ofh.write("\n")
    logging.info("Wrote %s" % outFname)

def buildLocusBed(db, geneType):
    " build a BED file with a 'canonical' transcript for every gene and a json file for it "
    if geneType.startswith("gencode"):
        release = geneType.split("-")[1]
        rows = iterGencodeBed(db, release)
    else:
        errAbort("Unknown gene model type: %s" % geneType)

    outFname = getStaticPath(getGeneBedPath(db, geneType))
    writeRows(rows, outFname)

    jsonFname = getStaticPath(getGeneJsonPath(db, geneType))
    bedToJson(db, geneType, jsonFname)

def listModelsLocal():
    " print all gene models on local machine "

    dataDir = join(findCbData(), "genes")
    logging.info("Local cell browser genes data directory: %s" % dataDir)
    fnames = glob.glob(join(dataDir, "*.symbols.tsv.gz"))
    names = [basename(x).split(".")[0] for x in fnames]
    print("Installed gene/symbol mappings:")
    print("\n".join(names))

    fnames = glob.glob(join(dataDir, "*.bed.gz"))
    names = [basename(x).replace(".bed.gz","") for x in fnames]
    print("Installed gene/location mappings:")
    print("\n".join(names))

def iterBedRows(db, geneIdType):
    " yield BED rows of gene models of given type "
    fname = getStaticPath(getGeneBedPath(db, geneIdType))
    logging.info("Reading BED file %s" % fname)
    with openFile(fname) as ofh:
        for line in ofh:
            row = line.rstrip("\n\r").split("\t")
            yield row

def parseApacheDir(lines):
    fnames = []
    for l in lines:
        if "<a href" in l:
            fname = l.split('">')[1].split("<")[0]
            fnames.append(fname)
    return fnames

def listModelRemote():
    " print all gene models that can be downloaded "
    urls = [("hg38", "https://hgdownload.cse.ucsc.edu/goldenPath/hg38/database/"),
            ("mm10", "https://hgdownload.cse.ucsc.edu/goldenPath/mm10/database/"),
            ("hg19", "https://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/")
            ]

    allNames = defaultdict(list)
    for db, url in urls:
        logging.info("Downloading %s" % url)
        lines = downloadUrlLines(url)
        fnames = parseApacheDir(lines)
        geneFnames = [x for x in fnames if x.startswith("wgEncodeGencodeAttrs") and x.endswith(".txt.gz")]
        relNames = [x.replace("wgEncodeGencodeAttrsV", "gencode-").replace(".txt.gz", "") for x in geneFnames]
        allNames[db].extend(relNames)

    for db, names in allNames.items():
        for name in names:
            print("%s\t%s" % (db, name))

def keepOnlyUnique(dictSet):
    """ give a dict with key -> set, return a dict with key -> set, but only with elements in the set that
    that don't appear in any other set
    """
    uniqVals = {}
    for key1, origVals in dictSet.items():
        vals = set(list(origVals))

        for key2 in dictSet.keys():
            if key1==key2:
                continue
            vals = vals - dictSet[key2]
        uniqVals[key1] = vals

    setList = list(dictSet.values())
    allCommon = set.intersection(*setList)
    return uniqVals, len(allCommon)

def writeUniqs(dictSet, outFname):
    " wrote to output file in format <key>tab<comma-sep-list of vals> "
    logging.info("Writing to %s" % outFname)
    with openFile(outFname, "wt") as ofh:
        for key, vals in dictSet.items():
            ofh.write("%s\t%s\n" % (key, "|".join(vals)))

def uniqueIds(org):
    """ find unique identifiers in all symbols and geneIds of infileMask and write to
    outBase.{syms,ids}.unique.syms.tsv.gz
    """
    logging.info("Processing: %s" % org)
    infileMask = "gencode*.symbols.tsv.gz"
    dataDir = join(findCbData(), "genes")
    fnames = glob.glob(join(dataDir, infileMask))
    allSyms = {}
    allIds = {}
    for fname in fnames:
        baseName = basename(fname)
        if "lift" in baseName or "mouse" in baseName or "human" in baseName:
            continue
        if org=="human" and "M" in baseName:
            continue
        if org=="mouse" and not "M" in baseName:
            continue
        geneType = basename(fname).split(".")[0]
        logging.info("Reading %s" % fname)

        syms = set()
        ids = set()
        for line in openFile(fname):
            row = line.rstrip("\n").split("\t")
            geneId, sym = row[:2]
            syms.add(sym)
            ids.add(geneId)
        allSyms[geneType] = syms
        allIds[geneType] = ids

    logging.info("Finding unique values")
    uniqSyms, commonSyms = keepOnlyUnique(allSyms)
    uniqIds, commonIds = keepOnlyUnique(allIds)
    logging.info("%d symbols and %d geneIds are shared among all releases" % (commonSyms, commonIds))

    writeUniqs(uniqSyms, join(dataDir, org+".syms.unique.tsv.gz"))
    writeUniqs(uniqIds, join(dataDir, org+".ids.unique.tsv.gz"))

def bedToJson(db, geneIdType, jsonFname):
    " convert BED file to more compact json file: chrom -> list of (start, end, strand, gene) "
    geneToSym = readGeneSymbols(geneIdType)

    # index transcripts by gene
    bySym = defaultdict(dict)
    for row in iterBedRows(db, geneIdType):
        chrom, start, end, geneId, score, strand = row[:6]
        sym = geneToSym[geneId]
        start = int(start)
        end = int(end)
        transLen = end-start
        bySym[sym].setdefault(chrom, []).append( (transLen, start, end, strand, geneId) )

    symLocs = defaultdict(list)
    for sym, chromDict in bySym.items():
        for chrom, transList in chromDict.items():
            transList.sort(reverse=True) # take longest transcript per chrom
            _, start, end, strand, transId = transList[0]
            symLocs[chrom].append( (start, end, strand, sym) )

    sortedLocs = {}
    for chrom, geneList in symLocs.items():
        geneList.sort()
        sortedLocs[chrom] = geneList

    ofh = open(jsonFname, "wt")
    outs = json.dumps(sortedLocs)
    #md5 = hashlib.md5(outs.encode("utf8")).hexdigest()[:10]
    ofh.write(outs)
    ofh.close()
    logging.info("Wrote %s" % jsonFname)

    #fileInfo[code] = {"label":label, "file" : jsonFname, "md5" :md5}

def buildGuessIndex():
    " read all gene model symbol files from the data dir, and output <organism>.unique.tsv.gz "
    dataDir = join(findCbData(), "genes")
    uniqueIds("human")
    uniqueIds("mouse")

def cbGenesCli():
    args, options = cbGenes_parseArgs()

    command = args[0]
    if command=="guess":
        fname = args[1]
        org = None
        if len(args)==3:
            org = args[2]
        guessGencode(fname, org)
    elif command=="syms":
        geneType = args[1]
        buildSymbolTable(geneType)
    elif command=="locs":
        db, geneType = args[1:]
        buildLocusBed(db, geneType)
    elif command=="fetch":
        db, geneType = args[1:]
        buildLocusBed(db, geneType)
        buildSymbolTable(geneType)
    elif command=="avail":
        listModelRemote()
    elif command=="ls":
        listModelsLocal()
    elif command=="index":
        buildGuessIndex()
    elif command=="json":
        db, geneType, outFname = args[1:]
        bedToJson(db, geneType, outFname)
    else:
        errAbort("Unrecognized command: %s" % command)

