# Loại tài liệu HSBA (TT 32/2023/TT-BYT) → resource FHIR VN Core

Bảng này trả lời: *phiếu này sinh ra resource gì, trường nào lấy ở đâu, cạm bẫy nào hay gặp*. Form key = giá trị
`documents[].form` trong clinical v2 (trùng khoá `vnlib.DOC_TYPES`). Mọi phiếu đều sinh **1 DocumentReference** (+ 1
Provenance gom các resource trích từ phiếu đó), nên dưới đây chỉ liệt kê resource lâm sàng.

| form | Mẫu / tên phiếu | LOINC DocumentReference.type | Resource trích xuất | Ghi chú |
|---|---|---|---|---|
| `phieu-kham-benh-vao-vien` | MS 29/BV-02 Phiếu khám bệnh vào viện | 34117-2 History and physical note | Patient (hành chính, HIS codes), Encounter.locations[clinic]+participant ADM, Condition role=admission (rank 1 — thường chỉ text, không mã), Observation vital-sign lúc khám, Observation exam (text), FamilyMemberHistory/AllergyIntolerance ("Tiền sử: Bt"), Location phòng khám | Mã HIS (nghề 00, dân tộc 01, xã/huyện/tỉnh cũ) **không phải** mã VN Core → tra `vn-province-cs`/`vn-ward-cs` theo tên; ô "Thu phí/BHYT" là trạng thái lúc đến khám, không phải nguồn thanh toán cuối |
| `giay-vao-vien` | Giấy vào viện | 34109-9 Note | — (thường scan) | `extracted=false`; ghi `note` lý do |
| `ket-qua-xet-nghiem-huyet-hoc` | Phiếu kết quả huyết học | 11502-2 Laboratory report | DiagnosticReport kind=lab (57021-8 + CLS 1000147), Specimen BLD (mã mẫu E…), Observation lab ×N, Practitioner KTV nhận mẫu + BS trả KQ, Organization khoa XN | Giờ: *chỉ định / nhận mẫu / trả KQ* → SR.authoredOn / DR.effective+Specimen.received / DR.issued+Obs.issued. Cờ H/L in cạnh giá trị. Tham chiếu theo giới ("Nam … Nữ …") |
| `ket-qua-xet-nghiem-sinh-hoa-mau` | Phiếu sinh hoá máu | 11502-2 | DR kind=lab (code **text-only** nếu không có panel LOINC khớp đúng bộ xét nghiệm), Specimen SER, Observation ×N | Mỗi xét nghiệm là 1 ServiceRequest riêng trên tờ điều trị → `based_on` theo tên; đơn vị quyết định LOINC (mmol/L vs mg/dL) |
| `phieu-xet-nghiem-nuoc-dich` | Phiếu nước tiểu / dịch | 11502-2 | DR kind=lab (50556-0 + CLS 1010290), Specimen UR, Observation ×N (test strip) | Âm tính → `value.string "Negative"`; ô trống → `absent not-performed`; ký hiệu máy ("Ca 70", "Dil", "A Norm : C") → ghi note, không suy diễn |
| `phieu-chup-cong-huong-tu` | MS 10/BV-02 Phiếu chụp CHT | 18748-4 Diagnostic imaging study | DR kind=imaging (LOINC + CLS 1070xxx), ServiceRequest CDHA, Practitioner BS đọc | `technique_text` = "Kỹ thuật"; `findings[]` = "Mô tả"; `conclusion` = "Kết luận". Giờ in trên phiếu thường là giờ **chỉ định** (xem pitfalls) |
| `phieu-sieu-am-doppler` | MS 19/BV-01 Phiếu siêu âm | 18748-4 | DR kind=imaging (43765-7 Doppler cảnh, 24733-8 xuyên sọ…) | Dòng kết luận hay lỗi font (PDF nhúng font) → lấy nguyên văn từ tờ điều trị / tóm tắt HSBA và ghi note |
| `phieu-chup-cat-lop-vi-tinh` / `phieu-chup-x-quang` | CT / X-quang | 18748-4 | DR kind=imaging | tra `term_lookup.py imaging` |
| `phieu-dien-nao-do` / `phieu-dien-tim` | TDCN | 11523-8 EEG study / 11524-6 EKG study | DR kind=functional (category OTH, nhóm chi phí 5) | không có mã CLS QĐ 1227 → chỉ LOINC |
| `phieu-cham-soc` | MS 38/BV1 Phiếu theo dõi & chăm sóc cấp 2–3 | 34746-8 Nurse Note | Observation vital-sign theo từng cột giờ (performer ĐD), Observation exam (tri giác, da, đau — text-coded), Location buồng/giường, CarePlan điều dưỡng (chẩn đoán ĐD, mục tiêu, can thiệp T/CLS/TD/C/HD), MedicationAdministration (cột T1 = thực hiện thuốc), AllergyIntolerance (ô "Chưa ghi nhận") | Tờ T3/T4 là phụ lục bảng mã, **không** chứa dữ liệu NB; mã T1/CLSx/TDx/Cx là quy ước nội bộ, chỉ HDx có giải nghĩa |
| `trich-bien-ban-hoi-chan` | MS 27/BV-02 | 11488-4 Consult note | ClinicalImpression (assessor = chủ toạ), Condition role=admission (kết luận hội chẩn), Encounter.participant CON, ServiceRequest từ "Hướng điều trị" | Tóm tắt bệnh án + khám là nguồn tốt cho `physical_exam` |
| `phieu-dieu-tri` | Tờ điều trị (mỗi ngày 1 tờ) | 11506-3 Progress note | Observation vital-sign (BS), Condition (diễn biến), ServiceRequest ×N (mục "Phiếu XN / CĐHA"), MedicationRequest (+ Medication), CarePlan y lệnh (ăn, chăm sóc cấp, theo dõi), MedicationAdministration (suy ra từ y lệnh + bảng kê, **flag**) | Dòng chẩn đoán HIS hay tự chèn tên ICD ("Các thoát vị…") — không tạo Condition thừa |
| `phieu-danh-gia-nguy-co-te-nga` | Phiếu nguy cơ té ngã | 34109-9 | Observation survey (điểm tổng, text-coded), performer ĐD | không bịa LOINC thang điểm nội bộ (Morse chỉ khi phiếu ghi rõ) |
| `phieu-sang-loc-dinh-duong` | MS DD-03 | 34109-9 | Observation vital-sign (CN, CC, BMI) + survey (điểm NRS) | bản trống điểm → chỉ làm DocumentReference |
| `ban-tom-tat-ho-so-benh-an` | MS 03/BV-TT25 Bản tóm tắt HSBA | 18842-5 Discharge summary | **Composition** (discharge_summary, sections LOINC), Condition role=discharge/comorbidity (có mã ICD HIS), Encounter.period/treatment_outcome/discharge_disposition, Patient CCCD | Tài liệu kết luận — ưu tiên khi mâu thuẫn với phiếu ban đầu |
| `phieu-cong-khai-dich-vu` | Công khai dịch vụ TT 50 | 51851-4 Administrative note | (đối chiếu) | có thể liệt kê vật tư không thu phí → không đưa vào Claim; chữ ký BS nội trú |
| `bang-ke-chi-phi-kcb` | Mẫu 01/KBCB (QĐ 6556) | 51851-4 | **Claim + Invoice + Account + Composition bảng kê** (bundle cost-statement), Coverage (thẻ, mức hưởng, nơi ĐKKCB), Condition discharge (mã ICD in sẵn), Encounter.identifier MA_LUOT_KCB / số lưu trữ | Mỗi dòng = 1 Invoice.lineItem + 1 Claim.item; tổng phải khớp từng đồng (`quality_check` kiểm). Ô chữ ký không in tên → Composition `preliminary` |
| `phieu-cls` | Phiếu CLS gộp (scan) | 34109-9 | — | thường scan, `extracted=false` |
| `giay-ra-vien` / `giay-chuyen-tuyen` | Giấy ra viện / chuyển tuyến | 18842-5 / 57133-1 Referral note | Encounter.discharge, Condition discharge; ServiceRequest referral (ext referralMode) | chưa có trong ca mẫu — tra `term_lookup.py doc-type` |

## Thứ tự ưu tiên khi các phiếu mâu thuẫn

1. Bảng kê chi phí (số tiền, mã ICD HIS, mã lượt, thẻ BHYT) và Bản tóm tắt HSBA (chẩn đoán, period, kết quả điều trị) — chứng từ kết luận.
2. Phiếu kết quả CLS gốc (giá trị, đơn vị, giờ) — hơn bản chép lại trên tờ điều trị, **trừ** khi phiếu gốc lỗi font.
3. Tờ điều trị / hội chẩn — y lệnh, chẩn đoán làm việc.
4. Phiếu khám vào viện / phiếu điều dưỡng — dữ liệu lúc đầu, có thể lỗi nhập (tuổi, giới checkbox).
Luôn ghi cặp giá trị + nguồn vào `conflicts[]` thay vì chọn im lặng.

## Thời điểm (effective) lấy ở đâu

| Resource | effective / authoredOn | issued |
|---|---|---|
| ServiceRequest | giờ in trên tờ điều trị / giờ "chỉ định" trên phiếu KQ | — |
| Specimen.received | giờ "nhận mẫu" | — |
| Observation lab | giờ **thực hiện / nhận mẫu** (không phải giờ trả KQ) | giờ "trả KQ / in" |
| DiagnosticReport | như Observation; imaging: giờ thực hiện nếu có, nếu chỉ có 1 giờ in → dùng + note | giờ ký |
| Observation vital-sign | cột giờ trên phiếu điều dưỡng / giờ khám | — |
| Condition.recordedDate | giờ của phiếu ghi chẩn đoán lần đầu (khám vào viện / hội chẩn / ra viện) | — |
| Encounter.period | "Vào viện … Ra viện …" trên tóm tắt HSBA / bảng kê (giờ phòng khám → Encounter.location clinic) | — |
| Invoice.date / Composition bảng kê | giờ in bảng kê ("Ngày … giờ …") | — |
Định dạng: `vn_datetime("08h34 02/02/2026") → 2026-02-02T08:34:00+07:00`; ngày không giờ → `YYYY-MM-DD`.
