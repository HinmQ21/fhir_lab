# Cạm bẫy đã gặp trên hồ sơ thật (và cách xử lý)

Mỗi mục: *hiện tượng → hậu quả nếu bỏ qua → cách làm*. Ca mẫu: BN001736827 (23 phiếu, BVĐK tỉnh Phú Thọ, 02–03/02/2026).

## Đọc phiếu

1. **Giờ in giống nhau trên nhiều phiếu CĐHA/TDCN (08h35)** → ngỡ là giờ thực hiện. Thực ra là giờ in phiếu chỉ định. Giữ làm
   `effective` nhưng ghi `note` + review_notes; nếu phiếu có "giờ thực hiện" riêng thì dùng giờ đó.
2. **Dòng "Kết luận" của phiếu siêu âm lỗi font** (ký tự rác) → LLM "bịa" kết luận. Lấy nguyên văn từ tờ điều trị / tóm tắt
   HSBA (bác sĩ chép lại), ghi rõ nguồn thay thế trong `note` và `sources`.
3. **Ký hiệu máy xét nghiệm**: "Ca 70" (Clinitek), "Dil", "A Norm : C", "Trace" → không phải số đo chuẩn. Ghi giá trị như in
   kèm note "cần xác minh"; không đổi thành âm tính.
4. **Ô kết quả trống nhưng dòng vẫn in** (Albumin niệu, A/C) → `value.absent: not-performed`, không tạo giá trị 0.
5. **Phiếu bản trống (dinh dưỡng bản 1) và bản hoàn chỉnh (bản 2)** → chỉ bản hoàn chỉnh sinh Observation; bản trống chỉ là
   DocumentReference + note.
6. **Trang phụ lục** (bảng mã T3/T4 của phiếu chăm sóc) chứa toàn bộ bảng mã HD/CLS → LLM tưởng là dữ liệu NB. Đánh dấu
   `extracted: false`, dùng làm từ điển giải mã HDx.
7. **Checkbox** ("Giới: Nam ☐ Nữ ☒", "Thu phí ☒ BHYT ☐") trích text được cả hai nhãn → giới/đối tượng sai. Đối chiếu với
   tóm tắt HSBA / bảng kê; ghi `conflicts`.
8. **Tuổi khác nhau giữa phiếu (43 vs 44)** → chỉ lưu `birth_date`; tuổi là giá trị dẫn xuất.
9. **HIS tự chèn tên ICD vào dòng chẩn đoán** ("…; Các thoát vị đĩa đệm cột sống cổ khác") → Condition thừa. Nhận ra bằng
   cách so với tên ICD của mã đã có.
10. **Mã HIS cũ cho địa chỉ / nghề** (tỉnh 12, xã 08, nghề 00) → không thuộc VN Core. Tra `vn-province-cs`/`vn-ward-cs` theo
    tên (ĐVHC sau sáp nhập 2025); nghề "Chưa xác định" chỉ để text.
11. **Phiếu công khai dịch vụ liệt kê vật tư không có trên bảng kê** (bơm tiêm) → không đưa vào Claim; ghi conflicts.
12. **Hai dòng bảng kê cùng giá, tên gộp** ("Doppler động mạch cảnh, doppler xuyên sọ" + "Siêu âm doppler xuyên sọ") → gán
    mỗi dòng một dịch vụ thật (có phiếu KQ tương ứng), ghi lý do.

## Mã hoá

13. **LOINC do LLM đề xuất sai một nửa**: # vs % (Neut# 770-8 ✗ → 751-8), mass vs molar (Calci 17861-6 ✗ cho mmol/L → 2000-8),
    sai check digit (32623-3 ✗), không tồn tại (47598-9). ⇒ bảng curated + `--unit` + check digit + NLM.
14. **Mã deprecated nhìn hợp lý** (18755-9 "MR report", 18760-9 "Ultrasound report", 21000-5 RDW-SD) → chỉ dùng khi không có mã
    thay thế; DocumentReference dùng 18748-4.
15. **Danh mục CLS QĐ 1227 trùng lặp 3 khối CBC** → chọn nhất quán khối đầu; ghi review_notes.
16. **ICD-10 VN có cả "M50.2" và "M502"** → chỉ dùng dạng có dấu chấm (`icd10_search` đã lọc).
17. **Đơn vị sinh hiệu bị dịch** ("lần/phút") → vi phạm fixedString `/min` của profile. Dùng `vnlib.VITALS` label.
18. **Panel sinh hoá không có LOINC khớp đúng bộ** → DR.code text-only, không ép 24323-8 (CMP) hay 24326-1.
19. **`insurance_visit_type` (trái tuyến)** là extension legacy (trước 01/07/2024) → không xuất; thể hiện qua Claim.subType
    (vn-patient-visit-type-cs) + review_notes vì là suy luận.
20. **Chuyên khoa / cấp KCB / loại hình cơ sở** không in trên hồ sơ → suy luận từ tên khoa/bệnh viện, luôn ghi review_notes.

## Cấu trúc FHIR

21. **Provenance trong bundle trỏ ra ngoài bundle** → lỗi reference. Builder cắt `target` theo nội dung bundle và kéo agent/
    entity (Practitioner, DocumentReference) vào bao đóng.
22. **Khoa không in mã cục bộ** → không tạo identifier slice localCode (cần assigner); chỉ `type` deptClass.
23. **Composition bảng kê**: thiếu tên người ký ⇒ `preliminary` + 1 attester preparer là cơ sở; `final` cần 3 vai.
24. **Dòng LDL/TG/Chol có "đơn giá BH" nhưng tỷ lệ 0, tự trả 100%** ⇒ scope 2 (ngoài phạm vi), `insurance_amount: 0`,
    allocation toàn bộ vào patient_self_pay; totals.non_covered = Σ các dòng này.
25. **MedicationAdministration lần 2 (16h) chỉ suy từ y lệnh + số lượng thanh toán** → tạo nhưng `note` "suy luận" và
    review_notes; nếu không có bất kỳ bằng chứng thực hiện (cột T1 / ký ĐD) thì không tạo.
26. **Namespace định danh cục bộ**: dùng cùng gốc với `meta.facility.mrn_system` (`http://his.<cơ sở>/sid/<tên>`), không
    dùng base_url FHIR. CCCD/BHYT/CSKCB dùng sid của VN Core, không assigner.

## Quy trình

27. **Sửa tay tệp FHIR để hết lỗi** → mất đồng bộ với clinical.json, lần dựng sau lỗi quay lại. Luôn sửa clinical.json.
28. **Chạy pipeline LLM trên dữ liệu thật rồi gửi lên cloud** → vi phạm PDPL. Dùng endpoint nội bộ / dữ liệu đã `deidentify.py`.
29. **Quên `sources`** → Provenance rỗng, GT không truy vết được; quality_check chặn.
30. **Console Windows cp1252** → UnicodeEncodeError khi in tiếng Việt; `vnlib` đã ép UTF-8, và luôn mở file với `encoding="utf-8"`.
