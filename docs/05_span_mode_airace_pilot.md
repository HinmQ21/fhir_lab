# 05 — Chế độ span của skill `vn-fhir-convert` trên bộ airace_eval_data (pilot 08/10/2026)

## Bộ dữ liệu
`C:\Users\ADMIN\Downloads\airace_eval_data`: 101 văn bản bệnh án (`input/*.txt`) + GT (`groundtruth/*.json`) gồm 3 loại thực thể
có vị trí ký tự: observation (sign/symptom + isNegation), diagnosis (ICD-10, AD/DD, rank 1/2, isFamily/isHistorical/isNegation),
medication (nhãn thuốc, liều, RxNorm IN). `evaluate.py`: ghép cặp IoU ≥ 0.5, điểm trường = 0.6·F1 + 0.4·thuộc tính,
tệp = 0.4 diag + 0.3 med + 0.3 obs. Assertion chỉ so **phần tử đầu**.

## Những gì thêm vào plugin
| Thứ | Mục đích |
|---|---|
| `references/span-extraction.md` | Quy trình + định dạng `mentions.json` + quy ước chú thích (rút từ GT các ca **ngoài** tập test) |
| `scripts/span_export.py` | `locate` (dòng + nguyên văn → offset; ICD chuẩn hoá + kiểm chứng; RxNorm qua RxNav), `view`, `score` (sao công thức evaluate.py) |
| `vnlib.py` | `icd10_verify/icd10_dotted`, tìm ICD cả trong bổ sung WHO 2019 (truy vấn tiếng Anh), `rxnorm_ingredient` + cache |
| `vendor/icd10/icd10_who2019_supplement.json` | 3 101 mã WHO 2019 (simple-icd-10) mà bản ICD-10 VN thiếu: chương R, V–Z… |
| `vendor/rxnorm/rxnav_cache.json` | Cache RxNav (chỉ tên hoạt chất rời máy) |
| `agents/span-extractor.md` | 1 agent / 1 văn bản |

Self-test (GT → mentions → locate → score): 0.91–0.95 do GT tự mâu thuẫn (mã không chấm ở 0003, span trùng lặp), RxNav khớp 18/19 hoạt chất.

## Kết quả pilot — 9 tệp `--file filter`, chạy mù (agent không đọc GT)
| | Điểm | F1 | P | R | Thuộc tính |
|---|---|---|---|---|---|
| **Tổng (macro)** | **0.800** | | | | |
| Observation | 0.817 | 0.706 | 0.622 | 0.820 | 0.983 |
| Diagnosis | 0.781 | 0.759 | 0.670 | 0.888 | 0.814 (ICD 0.75, rank 0.72, type 0.84) |
| Medication | 0.808 | 0.825 | 0.786 | 0.931 | 0.783 (RxNorm 0.72, liều 0.65) |

Định vị: 100 % mention khớp nguyên văn ở lần chạy đầu (0 NOT FOUND trên 9 tệp). Chi tiết: `runs/airace_pilot_2026-10-08/`.

## Lỗi chính (error_analysis.txt)
1. **Precision observation thấp** — chú thích nhiều hơn GT: mô tả vết thương/khám chi ("ngọn chi hồng ấm", "mạch mu chân bắt được",
   "lộ cân"), và span dài hơn GT ("đau nhiều cột sống ngực" vs GT "đau"; "liệt vận động hai chi dưới" vs "liệt"). GT ưa từ đầu ngắn.
2. **Diagnosis type**: 24 mention GT `null` bị gán AD (dòng "Chẩn đoán" ở phiếu phụ/CĐHA); **rank** lẫn 1↔2 (36) khi không có mục ra viện.
3. **Tách dòng chẩn đoán ghép** ("Đau cột sống ngực, liệt vận động hai chi dưới/ Tiền sử…") thành nhiều span → mất ghép cặp + sai ICD; GT giữ nguyên cụm.
4. **ICD khác thói quen GT**: I82.9 vs I80.2 (huyết khối TM sâu), T07 cho "chấn thương …", S52.5 vs S52.51 (GT dùng mã 4 ký tự).
5. **Thuốc**: GT để null cho thuốc phối hợp (Meyerexcold, Combikit) và một số chế phẩm; GT không chú thích thuốc của người nhà / iod phóng xạ.

## Việc tiếp theo
- Lặp hướng dẫn trên **tập dev** (các tệp ngoài filter, có GT) — không tinh chỉnh theo lỗi của 9 tệp test để tránh rò rỉ.
  Ứng viên: span triệu chứng ngắn (từ đầu), giữ nguyên dòng chẩn đoán ghép, type null cho dòng "Chẩn đoán" ngoài các mục chuẩn,
  mã 4 ký tự, RxNorm null cho thuốc đa hoạt chất.
- Chạy toàn bộ 101 tệp (≈ 70k token / tệp với agent hiện tại).
- `--loose` của evaluate.py cần `parent_unspecified_map.json` (không có trong gói).

## Chạy đủ 101 tệp (08/10/2026, cùng phiên bản hướng dẫn với pilot)

92 tệp còn lại chạy bằng 29+9 agent `span-extractor` (mỗi agent 1–4 tệp; một đợt bị ngắt do hết hạn mức phiên và đã chạy lại).
Sau khi xong, phát hiện lỗi định vị: mention ngắn ("ho") có thể khớp vào giữa từ ("Khoảng") → `span_export.py` nay ưu tiên khớp
trọn từ; toàn bộ 101 tệp được định vị lại từ `mentions.json` (0 NOT FOUND; self-test không đổi; điểm 9 tệp filter không đổi).

| Tập | n | evaluate.py final_score | Obs | Diag | Med |
|---|---|---|---|---|---|
| **Toàn bộ** | 101 | **0.808** | 0.808 (P 0.62 R 0.80) | 0.810 (P 0.73 R 0.90) | 0.813 (P 0.78 R 0.88) |
| filter (mù) | 9 | 0.800 | 0.817 | 0.781 | 0.808 |
| ngoài filter | 92 | ≈ 0.817 (script nội bộ) | 0.807 | 0.812 | 0.813 |

Lưu ý: 92 tệp ngoài filter **không mù hoàn toàn** ở mức hướng dẫn — quy ước trong `span-extraction.md` được rút từ thống kê GT
của chính các tệp này (agent không đọc GT). Ca tốt nhất 0106 0.936; kém nhất 0016 0.518, 0024 0.619, 0029 0.648.

### Lỗi chính trên 101 tệp (`runs/airace_full_2026-10-08/error_analysis.txt`)
1. **Diagnosis type**: 436 mention GT `null` bị gán AD (chủ yếu khối "[Chẩn đoán]" trong tờ điều trị, dòng chẩn đoán trên phiếu chỉ định)
   + 110 GT `null` bị gán DD ("Bệnh kèm theo" đứng sau mục ra viện). **Rank**: 269 GT "2" bị gán "1" (agent lan rank 1 ra mọi mention của bệnh chính).
2. **Observation precision 0.62**: span dài hơn GT ("đau" bị bỏ lỡ 334 lần vì agent lấy "đau bụng âm ỉ vùng thượng vị"…), chú thích mô tả khám mà GT không có.
3. **Thói quen mã ICD của GT** khác lựa chọn "đúng nhất": K37 cho viêm ruột thừa (agent K35.x), T19.8 cho "còn sonde niệu quản" (Z96.0/Z46.6),
   I84.2 cho trĩ vòng (K64.8), T79.4 cho sốc mất máu (R57.1), M51.2 thắt lưng (M51.1), I82.x cho huyết khối TM sâu (I80.2), E05.0 vs E05.9.
4. **RxNorm**: GT để null cho thuốc phối hợp (Meyerexcold, Effer-Acehasan, Combikit, Caldihasan…), agent gán hoạt chất đầu → mất điểm.
5. **isHistorical** bị gán thừa 126 lần (tiền sử phẫu thuật trong bệnh sử).

### Hướng cải thiện (chỉnh trên tập dev, đo lại trên filter)
- type null cho "[Chẩn đoán]" trong tờ điều trị và dòng chẩn đoán trên phiếu chỉ định; "Bệnh kèm theo" luôn null; rank 1 chỉ ở ô chẩn đoán chính.
- Span triệu chứng = từ đầu ngắn ("đau", "tê", "liệt") khi phần sau là vị trí/mức độ.
- Bảng "thói quen mã" dữ liệu hoá từ GT dev (text → mã đa số) làm ứng viên ưu tiên trong `term_lookup`.
- RxNorm: thuốc nhiều hoạt chất → null (hoặc tra SCD/BN đúng tổ hợp nếu bộ chấm khác yêu cầu).

## Bước cải thiện: profile bộ dữ liệu + hướng dẫn v2 (08/10/2026)

Nguyên tắc: chỉ học/chỉnh trên **92 tệp dev** (ngoài filter); 9 tệp filter chỉ dùng để đo một lần, mù.

**1. Profile học từ GT dev** (`scripts/span_profile.py`): script áp lên dự đoán, không cần chạy lại LLM.
`icd` (văn bản → mã GT đa số), `rx` (tên thuốc → RxCUI/null đa số; thuốc có "+" → null), `trim` (cắt span triệu chứng về tiền tố
có trong từ vựng GT), `drop` (bỏ mention dev hầu như luôn FP), `type` (nhãn vị trí → type đa số), `rank` (rank 1 chỉ cho văn bản trùng
chẩn đoán chính). Kiểm định chéo 2 phần trên dev (quality, công thức evaluate.py):

| Cấu hình | baseline | icd | rx | trim | drop | trim+drop | type | rank | **tất cả** |
|---|---|---|---|---|---|---|---|---|---|
| dev CV | 0.817 | 0.818 | 0.823 | 0.832 | 0.835 | 0.850 | 0.820 | 0.819 | **0.861** |

**2. Hướng dẫn v2** (`references/span-extraction.md`; v1 lưu trong `runs/airace_profile_2026-10-08/`): span triệu chứng ngắn,
không chú thích mệnh đề bình thường phụ, type theo nhãn cùng dòng (khối "[Chẩn đoán]" và "Bệnh kèm theo" → null), rank theo văn bản,
thuốc phối hợp → ingredient trống, thuốc người nhà vẫn chú thích (isFamily, isHistorical).

**3. Kết quả trên 9 tệp filter** (evaluate.py chính thức, profile học trên toàn bộ 92 tệp dev):

| Cấu hình | final_score | Obs | Diag | Med | Obs F1 | Diag F1 | ICD | rank | type chẩn đoán | RxNorm |
|---|---|---|---|---|---|---|---|---|---|---|
| v1 (pilot) | 0.800 | 0.817 | 0.781 | 0.808 | 0.71 | 0.76 | 0.75 | 0.72 | 0.84 | 0.72 |
| v2 | 0.820 | 0.838 | 0.790 | 0.844 | 0.74 | 0.76 | 0.68 | 0.86 | 0.99 | 0.93 |
| **v1 + profile** | **0.852** | 0.893 | 0.825 | 0.848 | 0.83 | 0.80 | 0.75 | 0.88 | 0.98 | 0.93 |
| v2 + profile | 0.848 | 0.891 | 0.815 | 0.851 | 0.83 | 0.80 | 0.69 | 0.86 | 0.99 | 0.93 |

Nhận xét:
- **Profile là bước có lợi nhất** (+0.05 trên filter, +0.044 CV dev). Hai số đo khớp nhau ⇒ không phải quá khớp.
- **Hướng dẫn v2 tự nó** +0.02 (type, rank, RxNorm đều tốt lên), nhưng phần lớn trùng với cái profile đã sửa; v2 + profile ≈ v1 + profile
  (chênh 0.004, trong mức dao động giữa 2 lần chạy LLM). ICD v2 thấp hơn (0.68 vs 0.75) do khác lựa chọn mã ở lần chạy mới, không do quy tắc.
- Khi **có GT dev** ⇒ dùng profile. Khi **không có GT** (bộ mới) ⇒ v2 là bản hướng dẫn tốt hơn.
- Bảng profile, dự đoán và mentions lưu ở `runs/airace_profile_2026-10-08/` (chứa văn bản bệnh án ⇒ nội bộ).
