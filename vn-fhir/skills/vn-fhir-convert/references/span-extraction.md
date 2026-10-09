# Span mode — trích xuất thực thể có vị trí (observation / diagnosis / medication)

Dùng khi đầu ra **không phải FHIR** mà là danh sách thực thể gắn vị trí ký tự trên văn bản bệnh án (bộ đánh giá NER/
entity-linking, ví dụ `airace_eval_data`: `input/HSBA-BN-XXXX.txt` + `groundtruth/HSBA-BN-XXXX.json`). Cùng nguyên tắc với
chế độ FHIR: LLM đọc và quyết định, **script** tính vị trí và kiểm chứng mã; không bịa mã.

## Quy trình

1. **Đọc văn bản kèm số dòng** (Read tool, hoặc `span_export.py view <input.txt>`). Đọc hết, theo thứ tự.
2. **Viết `mentions.json`** — mỗi mention mang `line` (1-based) + **chuỗi nguyên văn chép đúng từ dòng đó** + thuộc tính.
   Không tự đếm ký tự: vị trí do script tính. Chép đúng chữ hoa/thường, dấu, khoảng trắng của văn bản.
3. **`python -I scripts/span_export.py locate <input.txt> mentions.json pred.json --report report.json`**
   → định vị (exact → không phân biệt hoa thường → chuẩn hoá khoảng trắng → dòng lân cận → mờ ≥ 0.85),
   chuẩn hoá ICD-10 (dạng có dấu chấm, kiểm với ICD-10 VN + WHO 2019), tra RxNorm qua RxNav theo **tên hoạt chất**.
4. **Vòng sửa**: mọi `NOT FOUND` ⇒ sửa `text`/`line` cho đúng nguyên văn rồi chạy lại. `ICD unverified` ⇒ xem lại mã
   (`term_lookup.py icd10 "<tên>"`); giữ nếu chắc chắn là mã VN hợp lệ nhưng từ điển thiếu (vd I84.x trĩ, mã 5 ký tự),
   ngược lại sửa. `RX unresolved` ⇒ sửa `ingredient` (tên INN tiếng Anh) hoặc để trống nếu không phải thuốc có hoạt chất.
5. **Profile bộ dữ liệu** (khi có GT dev): `span_profile.py learn --gt <gt> --input <input> --pred <dev_pred> --exclude <test ids> -o profile.json`
   rồi `span_profile.py apply profile.json <pred> <pred_out> --input <input>`. Profile học *thói quen của bộ GT* (mã ICD/RxNorm ưa dùng,
   độ dài span triệu chứng, nhãn vị trí → type, mention hay bị coi là thừa) — phần "quirk" của bộ dữ liệu để script làm, hướng dẫn
   này chỉ giữ quy ước lâm sàng. Đo bằng kiểm định chéo trên dev trước khi áp lên test; không bao giờ học từ GT của tập test.
6. Chấm nhanh: `span_export.py score <gt.json> pred.json -v 20` (công thức giống `evaluate.py` của airace).

## Định dạng `mentions.json`

```json
{
  "observation": [{"line": 37, "text": "Không sốt", "type": "symptom", "assertion": ["isNegation"]}],
  "diagnosis":   [{"line": 21, "text": "Áp xe hậu môn trực tràng", "icd10_code": "K61.2", "type": "DD", "rank": "1", "assertion": []}],
  "medication":  [{"line": 88, "name": "Vitabactam 2g/1g (Cefoperazon + sulbactam)", "dosage": "truyền tĩnh mạch 02 lọ",
                   "ingredient": "cefoperazone", "type": "điều trị", "assertion": []}]
}
```

Mention lặp lại cùng chữ trên cùng dòng: liệt kê đủ số lần; script gán lần xuất hiện chưa dùng tiếp theo.
`assertion` được script sắp theo thứ tự cố định `isFamily, isHistorical, isNegation` (bộ chấm airace chỉ so phần tử đầu).

## Quy ước chú thích (v2 — rút từ GT + phân tích lỗi trên tập dev, không dùng 9 tệp test)

**Phạm vi chung**
- Chú thích **mọi lần xuất hiện**, ở mọi phiếu (bệnh án, phiếu khám vào viện, tờ điều trị, tổng kết…), kể cả câu lặp lại nguyên văn.
- **Không** chú thích: giá trị xét nghiệm, tên dịch vụ/chỉ định CLS, chế độ ăn/chăm sóc, "Bình thường" chung chung theo hệ cơ quan
  ("+ Tuần hoàn: Bình thường"), chiều cao, thông tin hành chính.

**observation** — `type`: `symptom` (bệnh nhân kể: đau, đau bụng, mệt mỏi, chóng mặt, buồn nôn, khó thở, sốt, ho, tê bì, ăn uống kém,
hạn chế vận động) hoặc `sign` (thầy thuốc khám thấy: bệnh nhân tỉnh, tiếp xúc tốt, da niêm mạc hồng, bụng mềm, tim nhịp đều,
phổi thông khí đều, ấn đau, phản ứng thành bụng, sưng nề, ran ẩm, khối ở hậu môn, vết mổ, xuất huyết dưới da…).
- **Khám bình thường vẫn chú thích** là `sign` không phủ định: "Bụng mềm", "Tim nhịp đều", "Bệnh nhân tỉnh", "Da niêm mạc hồng".
- **Sinh hiệu có giá trị** là `sign`, span gồm tên + số + đơn vị: "Mạch 80 lần/ph", "Nhiệt độ 37 ºC", "Huyết áp 120/80 mmHg",
  "Nhịp thở 18 lần/ph", "Cân nặng 58 kg", "SpO2 98%", "Glasgow 15 điểm". Không chú thích "Chiều cao".
- **Phủ định**: span gồm cả chữ "không": "Không sốt" (symptom), "không nôn", "không đau", "không phù" (sign), "Gan lách không to",
  "Hạch ngoại vi không to", "không liệt thần kinh khu trú", "không xuất huyết dưới da" → `assertion: ["isNegation"]`.
- **Span = cụm triệu chứng lõi, ngắn**: dừng trước vị trí giải phẫu chi tiết, mức độ, hoàn cảnh xuất hiện. Giữ danh từ cơ quan đã
  thành thuật ngữ (đau **bụng**, đau **đầu**, đau **ngực**, đau **lưng**, đau **họng**, đau tức ngực, đau bụng âm ỉ), bỏ phần định vị:
  "[đau bụng] thượng vị", "[đau bụng] hố chậu phải", "[ấn đau] thượng vị", "[tê bì] tay chân hai bên", "[đau] nhiều cột sống thắt lưng",
  "[ho] thúng thắng", "[nôn máu] đỏ tươi", "[ngứa] nhiều", "[khó thở] khi gắng sức", "[ran ẩm] rải rác", "[hồi hộp] tim đập nhanh".
  Khám: "[Khối sưng nề cạnh hậu môn] vị trí 6h, [ấn đau] nhiều", "[Tim nhịp đều] T1 T2 rõ", "[Phổi thông khí đều hai bên] không có ran".
  Lý do: bộ chấm ghép cặp theo IoU ≥ 0.5 — "đau bụng thượng vị" so với "đau bụng" chỉ đạt 0.44 ⇒ mất cả hai.
- **Đừng chú thích** các mệnh đề phụ mô tả bình thường đi kèm một dấu hiệu đã chú thích: "Bụng mềm, [không chướng]", "[tự thở]",
  "[không có ran]/[không rales]", "[không có tiếng tim bệnh lý]", "[đại tiểu tiện bình thường]", "[chạm thận (-)]",
  "[dấu hiệu thận to âm tính]". Chỉ phủ định một **triệu chứng/dấu hiệu bệnh có tên** mới chú thích ("không sốt", "không nôn", "không phù").
- Tách triệu chứng nối bằng dấu phẩy/"kèm": "[mệt mỏi], [ăn uống kém]".

**diagnosis** — tên bệnh/hội chứng/tình trạng (kể cả "Theo dõi: …", "Sau mổ …"), span là cụm chẩn đoán (bỏ "Bệnh chính:", mã in kèm).
- `type` theo **nhãn ngay trước chẩn đoán trên cùng dòng**: `AD` khi nhãn là "Chẩn đoán vào viện", "Khoa khám bệnh, Cấp cứu",
  "Khi vào khoa điều trị", "Chẩn đoán trước/sau phẫu thuật", "Chẩn đoán của nơi giới thiệu", "Nơi chuyển đến", "Chẩn đoán:" (dòng đầu
  tờ điều trị/phiếu khám), "Bệnh chính:" của phần bệnh án; `DD` khi nhãn là "Ra viện: Bệnh chính". `null` cho mọi chỗ khác, đặc biệt:
  **các dòng liệt kê dưới tiêu đề khối "[Chẩn đoán]"** trong tờ điều trị (mỗi bệnh một dòng, không có nhãn) → null;
  **"+ Bệnh kèm theo:"** ở bất kỳ đâu (kể cả ngay sau mục Ra viện) → null; kết luận CĐHA/nội soi, tóm tắt, bệnh sử, tiên lượng → null.
- `rank` gắn theo **văn bản**, không theo bệnh: chọn **một** cụm chẩn đoán chính = mục đầu tiên sau "Ra viện: Bệnh chính:" (không có mục
  ra viện thì mục đầu sau "Chẩn đoán vào viện"/"Bệnh chính"). Mọi mention có **văn bản giống hệt** cụm đó (không phân biệt hoa thường)
  → `"1"`, ở bất kỳ vị trí nào; mention cùng bệnh nhưng diễn đạt khác ("Theo dõi …", "Sau mổ …", thêm/bớt chữ) → `"2"`. Gia đình → `null`.
- `assertion`: tiền sử bản thân ("Bản thân: …", "Tiền sử: … đã điều trị") → `["isHistorical"]`, rank "2";
  gia đình ("Bố/Mẹ/Ông nội…: tăng huyết áp") → `["isFamily","isHistorical"]`, rank null;
  "Gia đình không ai mắc lao, viêm gan B" → `["isFamily","isHistorical","isNegation"]`, rank null.
- `icd10_code`: mã ICD-10 dạng có dấu chấm (K61.2, R10.4, Z86.1). Chọn mã cụ thể nhất phù hợp văn bản; mã chung theo thói quen VN:
  "đái tháo đường typ 2" E11.9, "rối loạn lipid máu" E78.5, "tăng huyết áp" I10, "tai biến mạch máu não" I64, "hen phế quản" J45.9,
  "trĩ độ II" I84.2 (mã VN cũ), "tiền sử phẫu thuật" Z98.8, "đau bụng" R10.4, "suy kiệt" R64. Không chắc ⇒ tra `term_lookup.py icd10`;
  không có mã phù hợp ⇒ `null` (script cũng sẽ tra lại theo tên).

**medication** — mỗi lần thuốc xuất hiện trong đơn/y lệnh/tiền sử dùng thuốc.
- `name`: **toàn bộ nhãn thuốc** như in: "Vitabactam 2g/1g (Cefoperazon + sulbactam)", "Sodium Chloride Injection(Natri clorid)0,9%/250 ml",
  "NaCl 0.9%" (khi dùng làm dung môi pha), "omeprazol 20 mg". Không gồm số thứ tự "(1)", "- ", số lượng "x 02 Lọ".
- `dosage`: chuỗi đường dùng + liều + số lượng đi kèm, chép gần nguyên văn ("truyền tĩnh mạch 02 lọ", "uống 01 viên x 03 lần/ngày, dùng 05 ngày",
  "20 mg/ngày", "tiêm truyền 04 chai, pha thuốc").
- `ingredient`: tên **INN tiếng Anh của hoạt chất** (cefoperazone, acetaminophen, sodium chloride, tranexamic acid, escin…);
  script tra RxCUI mức ingredient (IN). **Thuốc phối hợp nhiều hoạt chất** (nhãn có "+", vd "Meyerexcold (Paracetamol + phenylephrin …)"),
  chế phẩm không có hoạt chất xác định (huyết thanh, chế phẩm máu, dịch nuôi dưỡng hỗn hợp, đông dược) ⇒ để trống.
- Thuốc người nhà đang dùng (trong tiền sử gia đình) vẫn chú thích, `assertion: ["isFamily","isHistorical"]`, type "duy trì".
- `type`: "điều trị" (dùng trong đợt nằm viện) hoặc "duy trì" (thuốc đang dùng ở nhà/tiền sử); thuốc tiền sử → `assertion: ["isHistorical"]`.

## Giới hạn đã biết của GT airace (đừng "học" theo)

- GT không đầy đủ (bỏ sót triệu chứng), không nhất quán (cùng kiểu câu khi có khi không `isNegation`, E11.9 vs E14), có span trùng lặp.
- Một số tệp dùng mã ICD không dấu chấm (S060) hoặc RxNorm mức SCDC (315266) thay vì IN — dự đoán theo quy ước đa số (có chấm, IN).
- Bộ chấm ghép cặp theo IoU ≥ 0.5 trên vị trí: span dài/ngắn hơn GT quá nhiều sẽ mất cả F1 lẫn điểm thuộc tính.
