async function issue5538Audit(input) {
  const { instance } = await WebAssembly.instantiate(input.wasm, { env: emEnv, wasi_snapshot_preview1: wasi });
  Pdfium.initWith(instance.exports);
  Pdfium.wasmExports.FPDF_InitLibrary();
  pdfiumInitialized = true;
  const original = Pdfium.wasmExports;
  const live = new Map();
  Pdfium.wasmExports = {
    ...original,
    malloc(size) { const pointer = original.malloc(size); if (pointer) live.set(pointer, size); return pointer; },
    free(pointer) { live.delete(pointer); original.free(pointer); },
  };
  const snapshot = () => ({ liveInputAllocations: live.size,
    liveWrapperBytes: [...live.values()].reduce((a, b) => a + b, 0),
    virtualFiles: Object.keys(fileSystem.fn2context).length, pendingSources: documentDataSources.size });
  const cleanup = () => {
    for (const sourceId of [...documentDataSources.keys()]) releaseDocumentData({ sourceId });
    for (const file of Object.keys(fileSystem.fn2context)) fileSystem.unregisterFile(file);
    for (const pointer of live.keys()) original.free(pointer);
    live.clear();
  };
  const r01 = [];
  const s15 = [];
  try {
    const encrypted = new Uint8Array(input.encrypted);
    const marker = new TextDecoder('latin1').decode(encrypted).lastIndexOf('startxref');
    if (marker < 0) throw new Error('Encrypted fixture has no startxref');
    const large = new Uint8Array(encrypted.length + 2 * 1024 * 1024);
    large.set(encrypted.subarray(0, marker));
    large.fill(10, marker, marker + 2 * 1024 * 1024);
    large.set(encrypted.subarray(marker), marker + 2 * 1024 * 1024);
    for (const [label, bytes] of [['small-encrypted', encrypted], ['large-encrypted', large]]) {
      for (const mode of ['legacy-data', 'registered-data']) {
        cleanup();
        const sourceId = mode === 'registered-data' ? registerDocumentData({ data: bytes.buffer }).sourceId : null;
        const attempts = [];
        for (let i = 0; i < 5; i++) {
          const result = mode === 'registered-data'
            ? loadDocumentFromRegisteredData({ sourceId, password: 'wrong' })
            : loadDocumentFromData({ data: bytes.buffer, password: 'wrong' });
          if (result.errorCode !== 4) throw new Error('Wrong-password fixture did not return PDFium error 4');
          attempts.push({ errorCode: result.errorCode, ...snapshot() });
        }
        if (mode === 'registered-data') releaseDocumentData({ sourceId });
        r01.push({ fixture: label, mode, inputBytes: bytes.length, attempts,
          afterAbandonedOpen: snapshot(), wrapperAllocationMeasurementOnly: true, rssMeasured: false });
      }
    }
    cleanup();
    const uriAttempts = [];
    for (let i = 0; i < 5; i++) {
      const result = await loadDocumentFromUrl({ url: input.encryptedUrl, password: 'wrong', preferRangeAccess: true });
      if (result.errorCode !== 4) throw new Error('HTTP 200 fallback did not reach the encrypted PDF');
      uriAttempts.push({ errorCode: result.errorCode, ...snapshot() });
    }
    r01.push({ mode: 'real-http-200-fallback', inputBytes: encrypted.length, attempts: uriAttempts,
      afterAbandonedOpen: snapshot(), wrapperAllocationMeasurementOnly: true, rssMeasured: false });
    cleanup();
    const sourceId = registerDocumentData({ data: encrypted.buffer }).sourceId;
    loadDocumentFromRegisteredData({ sourceId, password: 'wrong' });
    const document = loadDocumentFromRegisteredData({ sourceId, password: 'user' });
    if (!document.docHandle) throw new Error('Correct-password control did not open');
    await closeDocument(document);
    r01.push({ mode: 'registered-correct-password-control', afterClose: snapshot() });

    const source = new Uint8Array(input.corpus);
    const blockSize = 65536;
    if (source.length < 512 * blockSize) throw new Error('S15 needs an actual PDF larger than 32 MiB');
    const pointer = original.malloc(32);
    if (!pointer) throw new Error('WASM output allocation failed');
    try {
      for (const blocks of [128, 256, 512]) {
        for (const order of ['sequential', 'permuted', 'cross-boundary']) {
          for (const instrumented of [false, true]) {
            const cache = new PdfRangeCache(input.corpusUrl, {}, false, undefined);
            cache.fileSize = source.length;
            for (let block = 0; block < blocks; block++) {
              cache.addData(block * blockSize, source.slice(block * blockSize, (block + 1) * blockSize));
            }
            let predicates = 0;
            if (instrumented) {
              cache.chunks.find = function(predicate) {
                return Array.prototype.find.call(this, (candidate) => { predicates++; return predicate(candidate); });
              };
            }
            const begin = performance.now();
            const reads = order === 'cross-boundary' ? blocks - 1 : blocks;
            for (let i = 0; i < reads; i++) {
              const block = order === 'permuted' ? (i * 37) % blocks : i;
              const offset = block * blockSize + (order === 'cross-boundary' ? blockSize - 8 : 0);
              if (cache.read(offset, pointer, 16) !== 1) throw new Error('Actual cache read failed');
              const actual = new Uint8Array(Pdfium.memory.buffer, pointer, 16);
              for (let byte = 0; byte < 16; byte++) {
                if (actual[byte] !== source[offset + byte]) throw new Error('Actual cache read bytes differ from corpus');
              }
            }
            s15.push({ blocks, blockSize, order, instrumented, actualClass: 'PdfRangeCache',
              reads, logicalReadBytes: reads * 16, chunkPredicateChecks: instrumented ? predicates : null,
              wallMsIncludingCopyAndParity: performance.now() - begin,
              retainedPayloadBytes: cache.payloadBytes, sourceIsRealPdfContent: true,
              instrumentedTimeIsUnmodifiedProductTime: false, allReadsCorrect: true });
            cache.dispose();
          }
        }
      }
    } finally {
      original.free(pointer);
    }
    return { measurementComplete: true, r01, s15, wasmMemoryBytes: Pdfium.memory.buffer.byteLength,
      nativeApi: 'bundled PDFium WASM', s15IsCacheComponentNotWholeApp: true };
  } finally {
    cleanup();
    original.FPDF_DestroyLibrary();
  }
}
