# Hành trình làm tay: từ bộ HSBA (zip PDF) đến FHIR VN Core

Ca mẫu: `data/sample/HSBA-BN001736827.zip` (23 PDF, bệnh nhân nội trú Khoa Ngoại Thần kinh, BVĐK tỉnh Phú Thọ, 02–03/02/2026).
Mục tiêu của tài liệu này: ghi lại **đã làm gì, quyết định ra sao, vấp phải gì** ở từng bước, để bước tiếp theo
(chuẩn hoá thành workflow portable) không phải khám phá lại.

Mã nguồn từng bước nằm ở `work/step1_extract.py` → `step2_clinical.py` → `step3_fhir.py` → `step4_validate.py`,
dùng chung `work/common.py` (LLM client, PDF text) và `work/terminology.py` (bảng mã).

```
zip ──► 00_raw/*.pdf,*.txt ──► 01_documents/*.data.json ──► 02_clinical/clinical.json ──► 03_fhir/{resources,bundle-*.json} ──► 04_validation/report.*
         (bước 1a: text)        (bước 1b: KIE theo template)    (bước 2: chuẩn hoá + mã hoá)     (bước 3: VN Core)                (bước 4: kiểm tra)
```

Kết quả cuối trên ca mẫu: 16 phiếu được trích xuất (7 phiếu bị bỏ theo yêu cầu/luật), clinical.json 151 KB,
**167 resource FHIR** (Composition 1, Patient 1, Encounter 1, Condition 4, Observation 92, DiagnosticReport 8,
ServiceRequest 18, Specimen 3, MedicationRequest 1, Coverage 1, Claim 1, Organization 3, Location 2, Practitioner 10,
PractitionerRole 3, DocumentReference 16, ClinicalImpression 1, FamilyMemberHistory 1), 2 Bundle (document + transaction),
**0 lỗi** khi kiểm tra offline theo snapshot VN Core 0.10.0. Tổng cộng 34 lần gọi LLM (91k token vào / 40k token ra).

---

## Bước 0 — Nắm nghiệp vụ VN Core trước khi viết code

Nguồn: https://hl7.org.vn/kien-thuc/fhir-trong-benh-an-dien-tu/ và package `hl7.fhir.vn.core#0.10.0` (vendor ở `vendor/vncore/`).

Những điều rút ra và dùng xuyên suốt:

- **Bệnh án điện tử = Bundle `document`**, Composition đứng đầu (invariant `vn-bundle-document-first`), các section theo LOINC
  (10154-3 lý do vào viện, 11348-0 tiền sử, 29545-1 khám, 29548-5 chẩn đoán, 30954-2 cận lâm sàng, 10160-0 thuốc,
  47519-4 thủ thuật, 18842-5 ra viện). Composition `final` **bắt buộc có attester**. Ngoài ra sinh thêm Bundle `transaction`
  (PUT theo `Type/id`) để đẩy lên server.
- **Định danh có "chủ cấp"**: MRN (`sid/mrn`), mã lượt KCB (`sid/ma-luot-kcb`), mã tổ chức nội bộ
  (`sid/organization-local-id`), mã vị trí, mã bệnh phẩm, số văn bản… đều **phải có `assigner`** (invariant `*-issuer-scoped`).
  CCCD phải 12 số, thẻ BHYT `^[A-Z]{2}[0-9]{13}$`, mã CSKCB 5 ký tự.
- **Mã bệnh**: `Condition.code.coding` phải có slice `icd10vn` với system `.../CodeSystem/vn-icd10-cs` (VN Core bind
  required tới `vn-icd10-vs`); thêm coding WHO `http://hl7.org/fhir/sid/icd-10` là tuỳ chọn nhưng nên có.
- **Xét nghiệm**: Observation lab phải có `category=laboratory`, code LOINC **hoặc** `vn-cls-cs` (QĐ 1227),
  kết quả số **phải có mã UCUM** (`vn-lab-observation-ucum`), status final phải có value hoặc dataAbsentReason.
  DiagnosticReport lab: `category` = v2-0074 `LAB` **và** `vn-cost-category-cs` `3`; CĐHA: `RAD` + `4`.
- **Sinh hiệu**: profile con (heart-rate, blood-pressure…) cố định LOINC + đơn vị UCUM (`/min`, `mm[Hg]`, `Cel`, `kg`, `cm`, `%`).
- **Practitioner**: nếu dùng chức danh "được quản lý" (`vn-regulated-practitioner-title-cs`) thì bắt buộc có số
  chứng chỉ hành nghề → phiếu không in CCHN nên chỉ ghi **học vị/học hàm** (`vn-qualification-cs`: BS, BS-CKI, BS-CKII, ThS, KTV…).
- **Cơ sở KCB**: profile `vn-core-kcb-organization` đòi giấy phép hoạt động → không có trên phiếu, dùng `vn-core-organization`
  + extension loại/tuyến (`vn-organization-type-cs` BV, `vn-healthcare-level-cs` TINH). Khoa dùng `vn-core-organization-department`
  với `vn-department-cs` (K20 = Khoa Ngoại thần kinh) và `partOf` ≠ chính nó.
- **Encounter**: `class` v3-ActCode `IMP`, `type` `vn-encounter-type-cs` 03 (nội trú), extension kết quả điều trị
  (`vn-treatment-outcome-cs`), tuyến BHYT (`vn-insurance-visit-type-cs`), hình thức đến (`vn-referral-mode-cs`),
  `hospitalization.dischargeDisposition` theo `vn-discharge-disposition-cs`.
- **Claim** (bảng kê chi phí): `diagnosis` phải là **CodeableConcept** có ICD-10 VN (`vn-claim-diagnosis-has-icd10` không
  chấp nhận `diagnosisReference`), đúng 1 `insurance.focal=true`, `item.category` theo `vn-cost-category-cs`, tiền VND.
- Profile `vn-core-related-person` **không có** trong package 0.10.0 → RelatedPerson dùng base R4.

## Bước 1 — Zip PDF → JSON tài liệu theo template TT32

### 1a. Phân loại và lấy text
- Phân loại bằng luật trên tên file (`FORM_RULES`), bỏ qua theo yêu cầu (giấy vào viện, `_cls`, té ngã, dinh dưỡng) và
  bỏ **trang phụ lục hướng dẫn** của phiếu chăm sóc (nhận diện bằng "BẢNG QUY ƯỚC MÃ HOÁ"/"HƯỚNG DẪN GHI PHIẾU").
  Trong pipeline thật, bước này do ClassifyRAG đảm nhiệm.
- Text PDF bằng pymupdf (`get_text("dict", sort=True)`), ghép span theo dòng.
- **Vấp 1 — font không có ToUnicode.** 5 phiếu CĐHA (MRI, siêu âm, điện não) của HIS in bằng font nhúng
  `ABCDEE+Times New Roman` (Type0/Identity-H, không ToUnicode) → text ra chuỗi GID (`&KөS&ӝQJ…`). Cách giải:
  font subset giữ nguyên GID gốc của Times New Roman, nên ánh xạ GID→Unicode qua cmap của `C:\Windows\Fonts\times.ttf`
  (fontTools); GID 3 = khoảng trắng. **Lưu ý quan trọng**: pymupdf trả `span["font"]` **đã bỏ tiền tố subset**, phải lấy danh
  sách font từ `page.get_fonts(full=True)` và chỉ giải mã font **không có khoá `ToUnicode`** (`xref_get_key`). Khi tôi lỡ
  giải mã cả font có ToUnicode (`FAAAAH+TimesNewRomanPSMT`), text của bản tóm tắt bệnh án thành rác và **model bịa ra một
  bệnh nhân khác** (xem Vấp 4).

### 1b. KIE bằng LLM theo template
- Prompt: TEMPLATE (các khoá là mô tả cách điền) + TEXT phiếu → model trả JSON đúng khung. Model: `glm53-flash` qua endpoint
  OpenAI-compatible (vLLM).
- **Vấp 2 — thinking.** Bật thinking: 30k token mà không ra đáp án. Tắt thinking nhưng model vẫn "suy nghĩ" trong content
  → JSON hỏng. **Giải**: `response_format: {"type":"json_object"}` (guided decoding của vLLM) ép token đầu tiên là `{`.
  Từ đó 100% lần gọi parse được; thêm `_repair_json` (nhờ model sửa cú pháp) làm dự phòng, lưu raw lỗi để chẩn đoán.
- **Vấp 3 — trôi tên khoá.** Model đổi "Đơn vị"→"Đơn vị tính", "Giá trị bình thường"→"Giá trị BT", "Họ tên"→"Họ và tên".
  **Giải**: hậu xử lý `reconcile_keys` (bảng đồng nghĩa + quy tắc "1 thiếu + 1 thừa cùng mức → đổi tên"), ghi vào `warnings`
  của manifest.
- **Vấp 4 — bịa khi text rác.** Khi text vào vô nghĩa, model điền một bộ hồ sơ hoàn chỉnh của "NGUYỄN THỊ HỒNG, BV Tân Phú".
  **Giải**: chốt chặn `check_patient` — tên người bệnh trên mọi phiếu phải trùng phiếu đầu tiên (`PATIENT_MISMATCH`).
  Trong workflow thật cần thêm so khớp mã BN/năm sinh và ngưỡng "tỷ lệ null" để phát hiện phiếu trích xuất hỏng.
- Checkbox: đọc từ text không tin được (thứ tự ô). Đã xác minh bằng toạ độ `get_text("words")` và bằng vision
  (ảnh trang gửi lên model): phiếu khám vào viện thực tế tích **"Thu phí"**, trong khi bảng kê có thẻ BHYT GD… 80%.
  → quy tắc ưu tiên nguồn: **bảng kê > tóm tắt HSBA > phiếu khám**.
- Cache theo hash payload (`output/_llm_cache/`) giúp chạy lại toàn bộ trong vài giây khi chỉ sửa hậu xử lý.

Đầu ra: `output/<case>/01_documents/<seq>_<form>.data.json` + `manifest.json` (form, lý do, số trang, sha1, warnings).

## Bước 2 — JSON tài liệu → clinical.json (trung gian)

Định dạng bám theo `data/sample/clinical.json` (giữ toàn bộ khoá của mẫu, thêm `service_requests`, `care_orders`, `meta`,
và các trường `*_iso`, `code_source`, `source` để truy vết). Việc chính là **gom nhiều phiếu về một sự thật** và **gắn mã**:

- Hành chính: tên/ngày sinh/CCCD/BHYT lấy chéo từ phiếu khám, tóm tắt, bảng kê; mã tỉnh HIS in "12" nhưng `vn-province-cs`
  Phú Thọ là **25** → không tin mã HIS, tra theo tên; xã "Bản Nguyên" có 2 mã trong `vn-ward-cs` (08527 thuộc tỉnh 25, 08539
  không có cha) → chọn theo tỉnh cha.
- Lượt KCB: mã lượt = "Số khám bệnh" 4001838; giờ vào/ra lấy từ bảng kê ("08 giờ 24 phút, ngày 02 / 02 / 2026" — phải thêm
  mẫu regex riêng cho dạng có dấu phẩy và khoảng trắng quanh "/"); kết quả điều trị lấy mã "(10)" trên bảng kê (2 = Đỡ);
  "Gia đình xin về" → hình thức ra viện 4 (Xin ra viện); tuyến: bảng kê tích "Trái tuyến" → 3.
- Chẩn đoán: vào viện "Đau đầu kéo dài CRNN / TD thoát vị đĩa đệm CSC" tách theo `/` và đánh dấu `provisional` khi có "TD";
  ra viện **ưu tiên cách HIS đã mã hoá trên bảng kê** ("Đau đầu/ Rối loạn tiền đình" = H81.8, "Thoát vị đĩa đệm CSC" = M50.2),
  chỉ phần không khớp mới tách tiếp và nhờ LLM mã hoá; mọi mã LLM đều phải có trong từ điển ICD-10 (QĐ 4469, 18.824 mã).
- Xét nghiệm: 52 chỉ số từ 3 phiếu; LOINC từ bảng curated (`LAB_LOINC`, ưu tiên đơn vị mol/L đúng với phiếu), còn lại LLM
  gợi ý rồi đối chiếu; đơn vị → UCUM (`G/L`→`10*9/L`, `µmol/L`→`umol/L`, `ºC`→`Cel`…); khoảng tham chiếu "( 3.4 - 9.7 )" → low/high;
  cờ H/L → interpretation. Phiếu nước tiểu không in giờ → lấy giờ phiếu cùng đợt và ghi chú.
- CĐHA/TDCN: 5 phiếu; tên dịch vụ → LOINC + mã CLS (QĐ 1227 có ConceptMap sang LOINC): MRI CSC 24935-9/1070466,
  MRI sọ não 24590-2, Doppler cảnh 43765-7/1071138, Doppler xuyên sọ 39289-4, điện não 11523-8. Bài học: fuzzy-match CLS
  từng gán "doppler mạch cảnh" vào mạch chậu (1071073) → curated phải thắng fuzzy, và fuzzy chỉ nhận khi ≥0.8.
- Y lệnh: HIS in `[Phiếu CĐHA:]`/`[Phiếu xét nghiệm:]` **trong cột "Diễn biến bệnh"** còn `[Đơn thuốc:]` ở cột "Y lệnh"
  → phải quét cả hai. 18 chỉ định, 1 đơn thuốc ("Vitamin 3B extra … x 02 Viên / Uống, 8h34 16h" → ATC A11DB, 2 lần/ngày).
- Sinh hiệu: 53 quan sát từ phiếu khám, 2 tờ điều trị và bảng theo dõi chăm sóc (mạch/nhiệt độ/HA/nhịp thở/cân nặng/chiều cao).

## Bước 3 — clinical.json → FHIR VN Core

Nguyên tắc: **id tất định** (`pat-<mrn>`, `enc-<ma-luot>`, `cond-ad-1`, `obs-lab-huyet-hoc-3`, `dr-img-2`, `docref-<file>`…)
để chạy lại cho cùng kết quả và PUT idempotent; mọi resource có `meta.profile` VN Core và narrative `text` sinh tự động;
cấu hình cơ sở (tên đầy đủ, mã tỉnh/xã, URL hệ thống định danh nội bộ) nằm trong `FACILITY` vì **không in trên phiếu**.

Chi tiết ánh xạ: xem `docs/02_anh_xa_vncore.md`. Một số quyết định đáng chú ý:

- Patient: 4 định danh (CCCD + v2-0203 `NI`, BHYT, MRN + `MR` có assigner, mã bệnh án), extension dân tộc (01 Kinh),
  nghề nghiệp (text vì mã HIS "00" không thuộc `vn-occupation-cs`), quốc tịch VN, địa chỉ có extension tỉnh 25 + xã 08527.
- Organization Sở Y tế: lúc đầu dùng `sid/organization-local-id` với `assigner.display` → vi phạm invariant
  (đòi `assigner.reference`) → chuyển sang hệ thống định danh của cơ sở.
- Mã CSKCB 5 ký tự của BVĐK Phú Thọ không in trên phiếu → để `FACILITY["ma_cskcb"]=None`, chỉ có mã nội bộ; nơi ĐK KCB
  ban đầu (25262 TYT xã Vĩnh Lại) đưa vào `vn-ext-primary-care-facility` dạng `Reference.identifier`.
- Kết quả số không thứ nguyên (pH, tỷ trọng nước tiểu) vẫn phải có UCUM → `[pH]` và `1`.
- `timing.repeat.timeOfDay` phải là `hh:mm:ss` ("8h34" → "08:34:00").
- Thuốc: route vừa SNOMED 26643006 vừa `vn-medication-route-cs` 1.01; liều/lần = số lượng ÷ số mốc giờ.
- Hội chẩn → ClinicalImpression (assessor = chủ toạ, kết luận, hướng điều trị); tiền sử gia đình "BT" → FamilyMemberHistory
  ghi chú; phiếu chăm sóc "chưa ghi nhận dị ứng" → (đã chuẩn bị) AllergyIntolerance NKA SNOMED 716186003 khi phiếu có ô này.
- DocumentReference cho **mọi PDF không bị bỏ** (16), LOINC loại tài liệu, hash SHA-1 base64, size, contentType; DiagnosticReport
  tham chiếu lại chính attachment này trong `presentedForm`.

## Bước 4 — Kiểm tra

Không có Java/Docker daemon để chạy HL7 validator, nên viết bộ kiểm tra offline dựa trên **snapshot StructureDefinition**
đã vendor (`work/step4_validate.py`): cardinality kể cả slice (phân biệt theo discriminator value/pattern/$this/type; với
extension khai báo qua `type.profile` thì so `url` với canonical), fixed/pattern, binding required tới ValueSet VN (tính từ
compose/CodeSystem đầy đủ trước, expansions-subset chỉ là mẫu), 48 invariant `vn-*` cài lại bằng Python, mã thuộc CodeSystem VN
(fragment → cảnh báo), tham chiếu nội bộ của Bundle, Composition đứng đầu, định dạng ngày giờ.

Kết quả: 254 invariant được đánh giá, 93 binding kiểm được, **0 lỗi**. Giới hạn: 251 binding tới ValueSet ngoài (LOINC/SNOMED/
UCUM/ATC) không kiểm offline; khi có mạng/Docker, chạy thêm:

```
docker run --rm -v "$PWD":/w eclipse-temurin:17 java -jar /w/vendor/validator_cli.jar \
  /w/output/BN001736827/03_fhir/bundle-document.json -version 4.0.1 -ig /w/vendor/vncore/package -tx n/a
```

## Bài học cho workflow portable

1. **Guided JSON là bắt buộc** với model nhỏ; thinking tắt hẳn; luôn lưu raw + usage; cache theo hash.
2. **PDF text có thể hỏng âm thầm** (font không ToUnicode) → phải có kiểm tra "tỷ lệ ký tự lạ" và đường OCR/vision dự phòng.
3. **LLM sẽ bịa khi đầu vào rác** → chốt chặn nhất quán (tên/mã BN/năm sinh giữa các phiếu) trước khi gộp.
4. **Một sự thật, nhiều nguồn**: định nghĩa thứ tự ưu tiên nguồn cho từng trường (bảng kê > tóm tắt > phiếu khám),
   và luôn ghi `source` để truy vết.
5. **Mã hoá = curated trước, ConceptMap sau, LLM cuối cùng và phải xác minh** trong từ điển (ICD-10, CLS, LOINC subset).
6. **Những thứ không in trên phiếu** (mã CSKCB, CCHN bác sĩ, giấy phép hoạt động, URL hệ thống định danh) phải là
   **cấu hình cơ sở**, không để model đoán.
7. Kiểm tra VN Core nên chạy ngay sau khi sinh FHIR; snapshot của IG đủ để tự kiểm phần lớn, phần thuật ngữ ngoài để validator chính thức.
