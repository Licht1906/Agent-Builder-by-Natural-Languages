# Agent Builder by Natural Languages: dựng agent từ mô tả ngôn ngữ tự nhiên

## Problem Statement

Mỗi đội sản phẩm trong tổ chức đều muốn có một AI agent riêng cho nghiệp vụ của mình, nhưng không đội nào có kỹ sư AI. Muốn có agent, họ phải xếp hàng chờ đội nền tảng, và mỗi lần dựng một agent mới mất nhiều tuần: chọn tool, viết prompt, thiết kế luồng, thử, sửa.

Người hiểu nghiệp vụ nhất — chính người trong đội sản phẩm — lại là người không thể tự dựng agent. Họ diễn đạt được rất rõ *"tôi cần một trợ lý đọc email khiếu nại, tra đơn hàng trong hệ thống, rồi soạn thư trả lời cho tôi duyệt"*, nhưng không có cách nào biến câu đó thành một agent đang chạy.

Ở phía ngược lại, tổ chức không thể chỉ mở cửa cho ai cũng tự tạo agent tuỳ ý. Một agent do người không chuyên dựng lên có thể gửi email thật cho khách hàng, gọi vào API nội bộ, hoặc rơi vào vòng lặp tool và đốt hết ngân sách LLM của cả tháng. Nên vấn đề không phải chỉ là *"làm sao dựng agent nhanh"* — mà là **làm sao để người không phải kỹ sư dựng được agent, mà tổ chức vẫn kiểm soát được nó làm gì.**

## Solution

Một web platform có hai vai trò: **Người tạo agent** và **Admin duyệt**.

Người tạo mô tả nghiệp vụ bằng tiếng Việt. Một **meta-agent** đọc mô tả, phát hiện những thông tin còn thiếu và **phỏng vấn** để lấp đúng những chỗ thiếu đó — không hỏi lại điều người dùng đã nói. Khi đủ thông tin, nó sinh ra một **AgentSpec**: một bản mô tả agent có cấu trúc gồm các bước, prompt của mỗi bước, tool mỗi bước được phép dùng, model mỗi bước, và guardrails.

AgentSpec **không phải code**. Nó là dữ liệu, được thi hành bởi một runtime cố định do đội nền tảng viết. Nhờ vậy mọi thứ tổ chức cần kiểm soát đều nằm trong tầm với: agent chỉ dùng được tool trong danh mục đã phê duyệt, mức rủi ro của mỗi tool do người viết code gán chứ không do LLM tự phong, và ngân sách token/thời gian mỗi lần chạy được runtime cưỡng chế.

Người tạo xem agent của mình dưới dạng **graph trực quan** — bấm vào một bước để đọc prompt và quyền của bước đó — rồi **tinh chỉnh bằng hội thoại** (*"bỏ bước 3"*, *"bước 2 thêm quyền tra cứu đơn hàng"*). Mỗi lần tinh chỉnh sinh ra một **version mới, bất biến**.

Trước khi xin duyệt, người tạo phải **chạy thử trong sandbox**: tool chỉ-đọc chạy thật để họ thấy agent có làm được việc không, còn tool có tác dụng phụ bị chặn và chỉ trả về mô phỏng — agent chưa duyệt không thể gửi email thật. Hệ thống **sinh test case** từ tiêu chí thành công mà chính người tạo đã khai trong lúc phỏng vấn, người tạo sửa lại test case nếu cần, rồi chạy **eval**.

Admin nhận một hồ sơ duyệt đầy đủ: spec, graph, transcript sandbox thật, điểm eval, danh sách quyền tool đã xin, domain đã xin, chi phí đo được và trần chi phí xấu nhất. Admin duyệt hoặc từ chối kèm lý do. Version đã duyệt được deploy, và từ đó bất kỳ ai trong platform đều **mở cửa sổ chat** nói chuyện với agent đó, hoặc gọi nó qua API.

Người tạo tinh chỉnh tiếp bao nhiêu cũng được mà không ảnh hưởng bản đang chạy — vì `deployed` chỉ là một con trỏ tới một version đã duyệt. Rollback là dời con trỏ về version cũ.

## User Stories

### Tạo agent và phỏng vấn

1. Là người tạo agent, tôi muốn nhập mô tả nghiệp vụ bằng tiếng Việt tự nhiên, để bắt đầu dựng agent mà không cần biết kỹ thuật.
2. Là người tạo agent, tôi muốn meta-agent chỉ hỏi tôi những thông tin còn thiếu, để tôi không phải trả lời lại những điều mình vừa viết trong mô tả.
3. Là người tạo agent, tôi muốn thấy mình còn bao nhiêu câu hỏi nữa, để biết còn bao lâu thì xong thay vì bị hỏi vô định.
4. Là người tạo agent, tôi muốn khi mô tả của tôi đã đủ chi tiết thì hệ thống bỏ qua phỏng vấn và sinh spec ngay, để không bị hỏi những điều vô nghĩa.
5. Là người tạo agent, tôi muốn xem lại toàn bộ thông tin hệ thống đã thu được về agent của mình, để kiểm tra nó có hiểu đúng ý tôi không.
6. Là người tạo agent, tôi muốn khai rõ *thế nào là một câu trả lời đúng* cho agent của tôi, để sau này chất lượng agent được đánh giá theo đúng tiêu chí của tôi.
7. Là người tạo agent, tôi muốn khai rõ những việc agent **tuyệt đối không được làm**, để nó không vượt ra ngoài giới hạn nghiệp vụ của tôi.
8. Là người tạo agent, tôi muốn phiên phỏng vấn được lưu lại nếu tôi đóng trang giữa lúc đang trả lời, để tôi quay lại tiếp tục chứ không phải làm lại.

### Xem và hiểu agent đã sinh ra

9. Là người tạo agent, tôi muốn nhận được một agent gồm nhiều bước rõ ràng, để hiểu nó xử lý việc của tôi theo trình tự nào.
10. Là người tạo agent, tôi muốn xem agent của mình dưới dạng sơ đồ luồng trực quan, để nắm được hình dạng của nó chỉ bằng cách nhìn.
11. Là người tạo agent, tôi muốn bấm vào một bước trên sơ đồ và đọc được prompt của bước đó, để biết chính xác bước đó được chỉ dẫn làm gì.
12. Là người tạo agent, tôi muốn thấy mỗi bước được phép dùng những tool nào, để biết agent của tôi có quyền hạn tới đâu.
13. Là người tạo agent, tôi muốn thấy mỗi bước dùng model nào và tối đa bao nhiêu vòng lặp, để hiểu vì sao nó nhanh/chậm và đắt/rẻ.
14. Là người tạo agent, tôi muốn khi meta-agent sinh ra một spec không hợp lệ thì nhận được thông báo dễ hiểu và được thử lại, để không gặp một trang lỗi trắng.

### Tinh chỉnh

15. Là người tạo agent, tôi muốn sửa agent bằng cách nói bằng lời (*"bỏ bước 3"*, *"thêm bước gửi thông báo cho quản lý"*), để không phải điền form kỹ thuật.
16. Là người tạo agent, tôi muốn mỗi lần tinh chỉnh chỉ thay đổi đúng phần tôi yêu cầu, để những bước tôi đang hài lòng không bị viết lại.
17. Là người tạo agent, tôi muốn thấy sơ đồ luồng cập nhật ngay sau mỗi lần tinh chỉnh, để thấy hiệu quả của câu mình vừa nói.
18. Là người tạo agent, tôi muốn xem lịch sử các phiên bản agent của mình, để lần lại được mình đã thay đổi những gì.
19. Là người tạo agent, tôi muốn so sánh hai phiên bản, để hiểu một thay đổi đã làm gì.
20. Là người tạo agent, tôi muốn tinh chỉnh mà **không** làm ảnh hưởng phiên bản đang chạy thật, để người dùng của tôi không bị gián đoạn trong lúc tôi đang thử nghiệm.
21. Là người tạo agent, tôi muốn biết rõ phiên bản nào đang chạy thật, để không nhầm giữa bản nháp và bản thật.

### Tool và quyền hạn

22. Là người tạo agent, tôi muốn xem danh sách tool hệ thống có sẵn kèm mô tả, để biết agent của mình có thể làm được những gì.
23. Là người tạo agent, tôi muốn thấy mức rủi ro của từng tool, để hiểu vì sao có tool phải xin phép kỹ hơn.
24. Là người tạo agent, tôi muốn thấy tổng hợp toàn bộ quyền mà agent của tôi đang xin, để tự thấy nó có xin quá nhiều hay không trước khi trình admin.
25. Là người tạo agent, tôi muốn khai báo những domain mà agent được phép gọi HTTP tới, để nối agent vào hệ thống nội bộ của đội tôi.
26. Là người tạo agent, tôi muốn không thể tự hạ mức rủi ro của một tool, để chính tôi cũng không vô tình phá vỡ kiểm soát của tổ chức.

### Chạy thử trong sandbox

27. Là người tạo agent, tôi muốn chạy thử agent với dữ liệu đầu vào của mình, để biết nó có thực sự làm được việc trước khi xin duyệt.
28. Là người tạo agent, tôi muốn tool tra cứu/tìm kiếm chạy thật trong lúc chạy thử, để kết quả tôi thấy là kết quả đáng tin.
29. Là người tạo agent, tôi muốn tool có tác dụng phụ **không** thực thi thật trong lúc chạy thử mà chỉ hiện ra nó *sẽ* làm gì, để tôi không gửi email thật cho khách hàng khi đang thử.
30. Là người tạo agent, tôi muốn xem transcript đầy đủ của lần chạy thử, để lần ra được agent sai ở bước nào.
31. Là người tạo agent, tôi muốn thấy bước nào đang chạy sáng lên trên sơ đồ theo thời gian thực, để theo dõi agent đang làm gì thay vì nhìn một vòng xoay chờ.
32. Là người tạo agent, tôi muốn thấy lần chạy thử tốn bao nhiêu token, bao nhiêu tiền, bao nhiêu giây, để biết chi phí thực tế.
33. Là người tạo agent, tôi muốn thấy trần chi phí xấu nhất của agent, để cam kết được với đội mình rằng nó không bao giờ vượt quá con số đó.
34. Là người tạo agent, tôi muốn ba con số chi phí được ghi nhãn rõ ràng là *ước lượng*, *trần đảm bảo*, và *đo thật*, để không nhầm một phỏng đoán là một cam kết.
35. Là người tạo agent, tôi muốn agent bị dừng lại và báo lỗi tử tế khi nó vượt ngân sách, để một vòng lặp tool không đốt hết ngân sách của đội tôi.
36. Là người tạo agent, tôi muốn các lần chạy thử của tôi hoàn toàn tách khỏi lịch sử hội thoại thật của agent, để dữ liệu thử nghiệm không lẫn vào dữ liệu production.

### Đánh giá chất lượng (eval)

37. Là người tạo agent, tôi muốn hệ thống tự sinh bộ test case từ tiêu chí thành công tôi đã khai, để không phải tự nghĩ ra cách kiểm tra agent.
38. Là người tạo agent, tôi muốn xem và **sửa** test case trước khi chạy, để không bị chặn bởi những test case mà tôi thấy là sai.
39. Là người tạo agent, tôi muốn chạy eval và xem kết quả từng case kèm lý do đạt/không đạt, để biết phải sửa gì.
40. Là người tạo agent, tôi muốn biết ngưỡng đạt là bao nhiêu và mình đang ở đâu so với ngưỡng, để biết còn cách đích bao xa.
41. Là người tạo agent, tôi muốn khi chưa đạt ngưỡng thì được giải thích rõ vì sao chưa xin duyệt được, để biết việc tiếp theo phải làm.

### Phê duyệt (HITL)

42. Là người tạo agent, tôi muốn gửi phiên bản của mình đi xin duyệt, để đưa agent vào chạy thật.
43. Là người tạo agent, tôi muốn hệ thống chặn tôi xin duyệt khi chưa chạy sandbox, để hồ sơ tôi trình lên luôn có bằng chứng.
44. Là người tạo agent, tôi muốn theo dõi trạng thái hồ sơ xin duyệt của mình, để biết đang chờ ai.
45. Là người tạo agent, tôi muốn nhận được lý do cụ thể khi bị từ chối, để sửa đúng chỗ thay vì đoán.
46. Là admin, tôi muốn xem hàng chờ các phiên bản đang xin duyệt, để biết việc của mình.
47. Là admin, tôi muốn xem trong **một** hồ sơ: spec, sơ đồ luồng, transcript sandbox, điểm eval, quyền tool đã xin, domain đã xin, chi phí đo được và trần chi phí — để quyết định mà không phải đi tìm thông tin ở năm chỗ.
48. Là admin, tôi muốn thấy nổi bật những tool rủi ro cao mà agent này xin, để tập trung chú ý vào đúng chỗ đáng lo.
49. Là admin, tôi muốn duyệt một phiên bản, để nó được phép chạy thật.
50. Là admin, tôi muốn từ chối kèm lý do bắt buộc, để người tạo biết phải sửa gì.
51. Là admin, tôi muốn duyệt vượt ngưỡng eval kèm lý do bắt buộc được ghi lại, để không bị một máy chấm điểm dao động chặn đường trong tình huống tôi hiểu rõ.
52. Là admin, tôi muốn cấu hình ngưỡng eval, để siết hoặc nới theo mức độ rủi ro của tổ chức.
53. Là admin, tôi muốn cấu hình danh sách model được phép dùng, để agent không dùng model chưa được kiểm chứng.
54. Là admin, tôi muốn xem lại lịch sử mọi quyết định duyệt, từ chối và vượt rào kèm lý do, để phục vụ truy vết và kiểm toán.

### Chạy thật và vận hành

55. Là người tạo agent, tôi muốn deploy phiên bản đã được duyệt, để nó bắt đầu phục vụ.
56. Là người dùng cuối, tôi muốn xem danh sách agent đang chạy mà tôi được dùng, để chọn đúng trợ lý cho việc của mình.
57. Là người dùng cuối, tôi muốn mở cửa sổ chat và nhắn tin với một agent, để dùng nó mà không cần cài gì.
58. Là người dùng cuối, tôi muốn agent nhớ những gì tôi đã nói trong cùng phiên, để tôi hỏi tiếp *"còn cái kia thì sao?"* mà nó vẫn hiểu.
59. Là người dùng cuối, tôi muốn mở một phiên mới với lịch sử trống, để bắt đầu một việc khác không bị nhiễu bởi việc trước.
60. Là người dùng cuối, tôi muốn thấy câu trả lời hiện dần ra, để không phải chờ trước một màn hình trống.
61. Là người dùng cuối, tôi muốn thấy agent đang gọi tool nào, để tin được nó dựa vào đâu mà trả lời.
62. Là người tạo agent, tôi muốn gọi agent của mình qua API, để nối nó vào sản phẩm của đội tôi.
63. Là người tạo agent, tôi muốn rollback về một phiên bản cũ đã được duyệt, để xử lý ngay khi bản mới có vấn đề mà không phải chờ duyệt lại.

### Đăng nhập và phân quyền

64. Là người dùng, tôi muốn đăng nhập, để hệ thống biết tôi là ai và tôi được làm gì.
65. Là người tạo agent, tôi muốn chỉ thấy và sửa được agent của mình, để không vô tình đụng vào agent của đội khác.
66. Là người dùng không phải admin, tôi muốn không truy cập được màn hình duyệt, để việc phê duyệt luôn nằm đúng vai trò.

### Quan sát vận hành

67. Là admin, tôi muốn mọi lần chạy — cả sandbox lẫn production — đều được trace lại, để điều tra được khi có sự cố.
68. Là admin, tôi muốn xem chi phí thực tế tích luỹ theo từng agent, để biết agent nào đang tốn tiền.

## Implementation Decisions

### Quyết định trung tâm: AgentSpec là dữ liệu, không phải code

Agent do người dùng tạo ra **không sinh code và không bao giờ được `exec`**. Nó là một tài liệu `AgentSpec` có cấu trúc, được validate bằng Pydantic, và được thi hành bởi **một runtime LangGraph cố định do đội viết**.

Đây là quyết định mà mọi thứ khác treo lên. Nó là lý do các ràng buộc an toàn khả thi: kiểm soát quyền tool trở thành một phép kiểm tra thành viên tập hợp; sandbox trở thành hoán đổi implementation của tool; HITL trở thành đọc một tài liệu có cấu trúc thay vì review code do máy sinh; và versioning trở thành lưu một bản ghi JSON bất biến.

Cái giá phải trả, ghi lại để không ai ngạc nhiên về sau: **meta-agent không thể phát minh topology tuỳ ý.** Nó chỉ lắp được những hình dạng runtime hỗ trợ.

### Hình dạng của AgentSpec

Đây là hợp đồng duy nhất giữa hai nửa hệ thống, nên nó được ghi ở đây dưới dạng cấu trúc thay vì diễn giải bằng văn xuôi:

```
AgentSpec
  name: str
  description: str
  slots: InterviewSlots          # thông tin thu được lúc phỏng vấn
  steps: list[Step]              # ít nhất 1, thi hành tuần tự
  guardrails: list[str]          # suy ra từ slots.prohibitions
  budget: Budget

InterviewSlots
  audience: str                  # ai dùng agent này
  inputs: str                    # dữ liệu đầu vào từ đâu
  capabilities: list[str]        # cần làm được những việc gì
  success_criteria: list[str]    # thế nào là trả lời đúng  → nuôi eval
  prohibitions: list[str]        # tuyệt đối không được làm  → nuôi guardrails

Step
  id: str
  title: str                     # nhãn hiển thị trên sơ đồ
  prompt: str
  model: str                     # phải nằm trong allowlist của admin
  tools: list[str]               # phải nằm trong tool registry
  max_iterations: int            # trần vòng lặp ReAct của bước này
  next: str | None               # None = bước kế tiếp trong danh sách

Budget
  max_total_tokens: int
  max_wall_seconds: int
```

`next` để mặc định `None` nghĩa là "bước tiếp theo trong danh sách". Trường này tồn tại **ngay từ đầu** dù MVP chỉ chạy tuyến tính, để việc thêm rẽ nhánh về sau là mở rộng chứ không phải phá vỡ hợp đồng.

Luật validate — cưỡng chế trong validator, không phải nhắc nhở LLM:

- mọi `Step.model` phải thuộc allowlist model do admin cấu hình;
- mọi tên trong `Step.tools` phải tồn tại trong tool registry;
- **một model thuộc nhóm "rẻ" không được gán cho một bước có tool** — đây là chỗ thiết kế này dễ hỏng nhất, vì nhiều model rẻ gọi tool không đáng tin và agent sẽ sai theo cách rất khó truy;
- nếu `tools` chứa `http_request` thì version phải khai kèm danh sách domain;
- `max_iterations` và `Budget` có trần cứng ở tầng hệ thống, spec không được vượt.

### Runtime interpreter

Một module dịch `AgentSpec` thành một `StateGraph` đã compile: mỗi `Step` thành một node; bước nào có tool thì node đó là một vòng ReAct nhỏ bị chặn bởi `max_iterations`; các cạnh nối theo `next` (mặc định là tuần tự).

Runtime **cưỡng chế ngân sách**: token và thời gian được cộng dồn xuyên suốt lần chạy, vượt trần thì dừng agent và trả về lỗi có cấu trúc. Đây là lý do trần chi phí là một cơ chế có hiệu lực chứ không phải một nhãn trên UI.

Runtime phát sự kiện theo từng node (`astream_events` của LangGraph) — cùng một dòng sự kiện phục vụ cả streaming chat và phủ trạng thái trên sơ đồ. Không xây thêm cơ chế theo dõi thực thi nào khác.

### Tool registry

Một danh mục đóng, khai báo trong code. Mỗi entry gồm: tên, mô tả, schema tham số, **mức rủi ro** (`low` / `high`), cờ có tác dụng phụ, một implementation thật và **một implementation mô phỏng**.

Ràng buộc lên registry: **thêm tool mới bắt buộc phải viết cả bản mô phỏng của nó.** Đây là ràng buộc lên người viết code, không phải lên LLM.

**Mức rủi ro do người viết code gán, LLM không được phép tự đánh giá.** Meta-agent chỉ được *chọn* tool.

Danh mục MVP gồm khoảng sáu tool (tìm kiếm web, truy vấn dữ liệu chỉ-đọc, đọc file, tính toán, gửi email, gọi HTTP) — trong đó `http_request` là **cửa thoát có kiểm soát**: nó khiến danh mục đóng vẫn phủ được gần như mọi nghiệp vụ, nhưng mỗi lần dùng đều phải khai domain và domain đó phải được admin duyệt cho từng version. Đây chính là thứ làm cho màn hình duyệt của admin có nội dung thật để duyệt.

### Sandbox

Sandbox **không** phải cách ly process — không có code nào được sinh ra nên không có gì cần cách ly. Thứ cần cách ly là **tác dụng phụ ra thế giới bên ngoài**.

Chế độ sandbox là một cờ trên lần chạy: tool chỉ-đọc chạy implementation thật; tool có tác dụng phụ chạy implementation mô phỏng và ghi vào transcript dạng `[SANDBOX] sẽ gửi email tới … với nội dung …`.

Transcript sandbox là **bằng chứng đính kèm bắt buộc** của hồ sơ xin duyệt.

Thread hội thoại của sandbox tách khỏi thread production, để dữ liệu thử không lẫn vào lịch sử thật.

### Vòng đời version

`Agent` là danh tính bền; `AgentVersion` là một `AgentSpec` **bất biến** kèm trạng thái. `Agent.deployed_version` là một **con trỏ** tới một version đã duyệt.

Trạng thái: `draft` → `sandbox_passed` → `pending_approval` → `approved` → (được trỏ tới) `deployed`; nhánh `rejected` kèm lý do.

Ba chuyển trạng thái được cưỡng chế ở tầng nghiệp vụ:

- **không được xin duyệt nếu chưa chạy sandbox** — nhờ vậy admin luôn có transcript;
- **không được xin duyệt nếu eval chưa đạt ngưỡng** — trừ khi admin dùng quyền vượt rào;
- **sửa spec không bao giờ thay đổi version đang chạy** — nó sinh một `draft` mới.

Rollback là một phép cập nhật con trỏ về một version `approved` cũ. Không có luồng nào cho phép sửa tại chỗ một version đã duyệt.

Đây là lý do phương án "duyệt agent một lần rồi cho sửa tự do" bị loại: nó cho phép xin duyệt một agent vô hại rồi thêm tool rủi ro cao sau khi đã được duyệt.

### Meta-agent

Một LangGraph riêng, **luôn dùng model mạnh do đội cố định** — cấu hình của người dùng không chạm tới được.

Phỏng vấn theo **slot-filling**: đọc mô tả ban đầu, tự điền những slot suy ra được, chỉ hỏi về slot còn trống, đủ slot thì dừng. Cơ chế dừng là xác định, không phụ thuộc phán đoán của LLM. Hệ quả: mô tả viết kỹ thì gần như không bị hỏi câu nào.

Sinh spec là structured output theo đúng Pydantic schema; spec không hợp lệ thì trả lỗi validate về cho vòng sinh lại, có giới hạn số lần thử.

Tinh chỉnh dùng **cùng một lời gọi đó** với input là *"spec hiện tại + yêu cầu sửa → spec mới"*. Không có đường sửa thứ hai: sơ đồ luồng là **chỉ đọc**, và không có form editor. Lý do là để tránh hai đường sửa song song ghi đè lẫn nhau, và vì một form editor cho cấu trúc lồng nhau tốn nhiều công frontend hơn hẳn một ô chat.

### Eval

Test case sinh từ `slots.success_criteria` — tiêu chí do **chính người tạo** khai lúc phỏng vấn. Đây là lý do khâu phỏng vấn tồn tại: nó không chỉ để hiểu ý người dùng mà là chỗ **thu thập tiêu chí eval**, việc không khâu nào khác làm được.

Test case **người tạo xem và sửa được trước khi chạy** — một vòng LLM tự sinh, tự chấm, tự chặn mà con người không can thiệp được là không chấp nhận được.

Eval là **cổng chặn mặc định** với ngưỡng thấp, admin cấu hình được (mặc định ~60% case đạt). Admin có **quyền vượt rào kèm lý do bắt buộc ghi lại** — vì người chấm là một LLM và nó dao động; không có van an toàn thì một lần chấm hụt sẽ chặn cứng cả luồng vận hành. Bản ghi lý do vượt rào là một artifact kiểm toán.

### Model theo từng bước

Model được chọn **theo từng bước**, meta-agent tự gán theo độ khó của bước, trong allowlist do admin cấu hình. Allowlist chỉ chứa model đã được kiểm chứng là gọi tool đáng tin.

Đây là chỗ câu chuyện chi phí trở nên thật: bước trích xuất dùng model rẻ, bước suy luận dùng model mạnh, và mức tiết kiệm **đo được** từ lần chạy sandbox.

**Một hàm duy nhất trong toàn hệ thống được phép khởi tạo chat model** — `get_chat_model(model_id) -> BaseChatModel`, bọc `init_chat_model` của LangChain, khai báo dạng FastAPI dependency. Đây vừa là nơi hiện thực "đa provider", vừa là seam test duy nhất cho LLM (xem Testing Decisions).

### Ba con số chi phí

Ba con số **khác nhau về bản chất** và phải được ghi nhãn khác nhau trên UI:

| Con số | Tính từ | Bản chất |
|---|---|---|
| Ước lượng | số bước × token giả định × giá | phỏng đoán |
| **Trần xấu nhất** | số bước × `max_iterations` × token tối đa × giá | **đảm bảo** — runtime cưỡng chế |
| Đo thật | lần chạy sandbox (Langfuse) | số liệu |

Việc gộp ba con số này thành một là chỗ dễ mất uy tín nhất: một con số "dự kiến" là phỏng đoán và sẽ sai, còn một con số "trần" là phát biểu đúng về mặt toán học.

### Lưu trữ

PostgreSQL. Các bảng chính: `users`, `agents` (kèm con trỏ `deployed_version_id`), `agent_versions` (spec dạng `jsonb`, trạng thái), `sandbox_runs` (transcript, token, chi phí, độ trễ), `eval_runs` (test case, kết quả, ngưỡng), `approvals` (quyết định, lý do, cờ vượt rào), `domain_allowlists` (theo version), `settings` (ngưỡng eval, allowlist model).

Trạng thái hội thoại của agent đã deploy dùng **LangGraph Postgres checkpointer**, mỗi cửa sổ chat là một thread. Bộ nhớ chỉ trong phiên; không có bộ nhớ dài hạn xuyên phiên. Thread sandbox tách namespace khỏi thread production.

`requirements.txt` hiện đang comment SQLAlchemy/psycopg2 — cần bật, và thêm Alembic cho migration.

### Hợp đồng API

Tất cả dưới `/api/v1`. Streaming dùng SSE.

| Nhóm | Endpoint |
|---|---|
| Tạo | tạo agent từ mô tả (trả về slot còn trống); trả lời slot phỏng vấn; sinh spec khi đủ slot |
| Tinh chỉnh | gửi yêu cầu sửa dạng ngôn ngữ tự nhiên → version draft mới |
| Đọc | danh sách agent; chi tiết version (spec + payload sơ đồ + ba con số chi phí); danh sách version; so sánh hai version; danh mục tool kèm mức rủi ro |
| Sandbox | chạy sandbox (SSE, phát sự kiện theo node); đọc transcript một lần chạy |
| Eval | sinh test case; sửa test case; chạy eval; đọc kết quả |
| Duyệt | xin duyệt; hàng chờ của admin; duyệt / từ chối / vượt rào |
| Vận hành | deploy version đã duyệt; rollback; chat với agent đã deploy (SSE); invoke dạng API |
| Cấu hình | ngưỡng eval; allowlist model |

Route `/api/v1/chat` hiện có trong template là code mẫu — sẽ bị thay.

### Xác thực

JWT đơn giản, hai vai trò `creator` và `admin`, tài khoản seed sẵn. Không OAuth, không tổ chức/nhóm. Người tạo chỉ thấy agent của mình; màn hình duyệt chỉ admin vào được.

### Frontend

Next.js. Sơ đồ luồng dùng **React Flow (`@xyflow/react`) — chỉ đọc**: bấm node để xem prompt, tool, model, `max_iterations`. Rồi cộng thêm **phủ trạng thái chạy thật**: node đang thực thi sáng lên, tool call hiện trên node, chi phí đếm tăng — nối từ dòng sự kiện `astream_events` qua SSE.

**Langflow bị loại.** Ba lý do: canvas của Langflow chính là React Flow nên dùng trực tiếp thư viện đó cho đúng thứ đang muốn mà không phải nhận cả một ứng dụng kèm theo; Langflow không cho nhúng canvas (thứ nhúng được là widget chat), nên sẽ phải chạy thêm một service + DB + hệ auth phải đồng bộ; và quan trọng nhất, cho **sửa** flow bên trong Langflow sẽ đưa Langflow lên làm nguồn chân lý và làm runtime, phá vỡ toàn bộ kiểm soát ở trên — allowlist tool bị đi vòng (người dùng kéo được component bất kỳ), bản mô phỏng sandbox không được áp dụng, tính bất biến của version mất, trần chi phí không được cưỡng chế.

Sơ đồ luồng **chỉ để đọc, không để sửa**. Kéo-thả để chỉnh sửa là một sản phẩm riêng (undo, validate, đồng bộ hai chiều với spec) và nó cạnh tranh trực tiếp với đường tinh chỉnh bằng hội thoại.

### Quan sát vận hành

Langfuse cho mọi lần chạy, cả sandbox lẫn production. Số liệu token/chi phí/độ trễ của hồ sơ duyệt đọc từ đây.

## Testing Decisions

### Thế nào là một test tốt ở đây

Test **chỉ quan sát hành vi ngoài qua HTTP**. Không test trực tiếp vào runtime interpreter hay meta-agent, không assert cấu trúc node bên trong graph đã compile, không assert prompt cụ thể. Cái được khẳng định là những điều mà một người dùng thật quan tâm: *xin duyệt khi chưa chạy sandbox thì bị chặn*, *chạy sandbox thì tool gửi email không thực sự được gọi*, *sửa spec không làm đổi version đang chạy*, *vượt ngân sách thì lần chạy dừng và trả lỗi có cấu trúc*.

Điều này quan trọng vì phần lõi (cách runtime dựng graph, cách meta-agent prompt) **sẽ** được viết lại nhiều lần trong quá trình làm; test bám vào hình dạng bên trong sẽ phải sửa theo mỗi lần và sẽ bị bỏ.

### Seam 1 — HTTP (dùng lại seam có sẵn)

Fixture `client` trong `tests/conftest.py` đã cấp `AsyncClient` qua `ASGITransport` vào `src.main:app` — test ở tầng HTTP không cần network. Đây là seam cao nhất repo đang có và **không thêm seam mới nào ở tầng này**.

Mọi user story chạy qua đây, theo đúng vòng đời: tạo → trả lời slot → nhận spec → tinh chỉnh → chạy sandbox → eval → xin duyệt → duyệt/từ chối/vượt rào → deploy → chat → rollback.

Prior art: `tests/test_api/test_routes.py` (dùng fixture `client`, assert status code và thân JSON).

### Seam 2 — model factory (seam mới duy nhất)

Vấn đề: LLM không xác định, và hệ này gọi LLM ở **hai** chỗ (meta-agent sinh spec; runtime thực thi từng bước). Nếu mỗi chỗ tự khởi tạo model thì có hai chỗ phải giả lập, và test sẽ hoặc gọi API thật (chậm, tốn, flaky) hoặc phải monkeypatch rải rác.

Vì vậy **đúng một hàm được phép tạo chat model** — `get_chat_model(model_id)` — khai báo dạng FastAPI dependency và ghi đè trong test qua `app.dependency_overrides` bằng model giả có kịch bản định trước (`FakeListChatModel` / `GenericFakeChatModel` của LangChain). Một chỗ ghi đè phủ cả hai đường gọi LLM.

Seam này **dù sao cũng phải có** vì lý do thiết kế (model theo từng bước, đa provider, ở mục Implementation Decisions) — nó chỉ tình cờ đúng luôn là điểm test tốt nhất. Đó là lý do tổng số seam giữ được ở mức hai.

### Không có seam cho tool

Bản mô phỏng của tool là **tính năng production**, không phải hạ tầng test. Test chạy ở chế độ sandbox và dùng luôn cơ chế mô phỏng thật — không mock tool, không thêm seam. Đây là cổ tức của quyết định thiết kế sandbox.

### Database trong test

**PostgreSQL thật**, service trong `docker-compose` và service container trong CI, schema mới mỗi lần chạy.

Không dùng SQLite: LangGraph checkpointer là bản Postgres, nên test trên SQLite sẽ đang test một hệ khác với hệ chạy thật, và đúng phần trạng thái hội thoại — chỗ dễ hỏng nhất khi deploy — sẽ không được phủ ở đâu cả. Cái giá là test chậm hơn và CI phức tạp hơn; chấp nhận.

### Những gì được test

- **Vòng đời version và các cổng chặn** — trọng tâm. Ba luật cưỡng chế ở mục vòng đời, cộng rollback và tính không-đổi của version đang chạy.
- **Cưỡng chế quyền tool** — spec xin tool ngoài registry hoặc `http_request` không khai domain thì bị từ chối.
- **Hành vi sandbox** — tool chỉ-đọc chạy, tool có tác dụng phụ không chạy và có mặt trong transcript.
- **Cưỡng chế ngân sách** — lần chạy vượt trần bị dừng, lỗi có cấu trúc.
- **Luật validate của spec** — đặc biệt luật "model rẻ không được gán cho bước có tool".
- **Slot-filling dừng đúng** — mô tả đầy đủ thì không hỏi câu nào; mô tả sơ sài thì hỏi đúng những slot trống.
- **Cổng eval và quyền vượt rào** — dưới ngưỡng thì chặn; vượt rào không kèm lý do thì bị từ chối.
- **Phân quyền** — người tạo không đọc được agent của người khác; không phải admin thì không vào được luồng duyệt.
- **Bộ nhớ hội thoại** — lượt thứ hai trong cùng thread thấy được ngữ cảnh lượt đầu; thread mới thì không; thread sandbox không lẫn vào production.

### Fixture xuất phát

`AgentSpec` **viết tay** từ mốc tracer bullet trở thành fixture dùng chung cho cả hai cặp — nó là hợp đồng ở dạng sống, chạy được, có test, thay vì một file schema mà mỗi bên hiểu một kiểu.

## Out of Scope

- **Rẽ nhánh / DAG tuỳ ý trong agent sinh ra.** MVP chỉ chạy pipeline tuyến tính. Trường `next` đã tồn tại trong schema để việc thêm rẽ nhánh sau này là mở rộng, không phải phá vỡ hợp đồng. Lý do hoãn: validate đích cạnh và phát hiện chu trình là một hạng mục riêng, và rẽ nhánh làm eval khó hẳn lên vì mỗi nhánh là một đường thực thi phải phủ.
- **Sửa agent bằng kéo-thả trên sơ đồ.** Sơ đồ chỉ đọc. Sửa đi qua hội thoại.
- **Form editor cho spec.** Cùng lý do.
- **Bộ nhớ dài hạn xuyên phiên** cho agent đã deploy. Tốn nhiều ngày (quyết định khi nào tóm tắt, truy hồi ra sao, khử trùng lặp) mà nhìn không khác gì bộ nhớ trong phiên.
- **Người tạo tự đăng ký tool riêng** (dán OpenAPI spec / URL + auth header). Kéo theo lưu credential của người dùng, chống SSRF, parse OpenAPI nhiều phiên bản. `http_request` giới hạn theo allowlist domain đã phủ phần lớn nhu cầu.
- **Sinh code / export ra Docker image để đội khác tự chạy.** Agent chạy trong platform.
- **Langflow ở mọi hình thức nhúng hoặc thực thi.** Xem mục frontend.
- **Thư viện mẫu agent tái sử dụng** ("memory tái sử dụng mẫu" ở phần Nâng cao của đề bài). Chưa thiết kế, ngoài MVP.
- **Export một chiều sang Langflow flow JSON.** Một hàm chuyển JSON thuần, không chạy, không sửa, không nhúng — an toàn về mặt kiến trúc nhưng vẫn là hạng mục Nâng cao, không thuộc MVP.
- **OAuth, tổ chức/nhóm, phân quyền chi tiết.** JWT + hai vai trò seed sẵn là đủ.
- **Đa người dùng đồng thời sửa cùng một agent**, khoá bản ghi, giải quyết xung đột.
- **Tối ưu chi phí ở tầng hệ thống** (cache prompt, batch, định tuyến giữa các provider).

## Further Notes

### Chia việc cho bốn người

Hai cặp, mỗi cặp sở hữu **cả frontend lẫn backend của nửa mình**, nên không ai phải chờ ai:

- **Cặp "sinh agent"** — meta-agent, phỏng vấn slot-filling, tinh chỉnh bằng chat, wizard UI, sơ đồ React Flow.
- **Cặp "chạy & quản trị"** — runtime interpreter, tool registry và bản mô phỏng, sandbox, eval, hàng chờ duyệt, chat production.

Cách chia này chỉ hoạt động nếu **ngày đầu tiên cả bốn người cùng đóng băng hai bản hợp đồng** trước khi ai viết logic gì: (1) Pydantic schema của `AgentSpec`, (2) tool registry kèm mức rủi ro. Đó là toàn bộ mặt tiếp xúc giữa hai nửa. Xong hai thứ đó thì hai cặp gần như không cần nói chuyện với nhau tới lúc ghép, và lúc ghép sẽ chạy.

Đây là lý do kiến trúc "spec khai báo" đáng giá gấp đôi: nó vừa là biên an toàn, vừa là đường chia việc.

Cách chia bị loại: **theo tầng** (frontend / backend / meta-agent / runtime) — người frontend chờ API, người meta-agent không chạy thử được vì runtime chưa xong, và cả nhóm gặp nhau lần đầu ở tuần cuối; **theo luồng dọc** (mỗi người một tính năng end-to-end) — cả bốn người cùng sửa runtime interpreter, xung đột merge liên tục và không ai sở hữu thiết kế phần lõi.

### Thứ tự làm: viên đạn vạch đường

Mốc chạy-được đầu tiên là **xuyên từ giữa ra**: một `AgentSpec` **viết tay** (chưa có meta-agent nào) → runtime chạy được spec đó với hai tool → chat được với nó qua UI.

Thứ tự này đảo ngược rủi ro. Nếu runtime chạy được một spec viết tay thì schema đã được **chứng minh là thi hành được**, và meta-agent thu về đúng bản chất của nó: một bộ sinh JSON theo schema đã biết chắc là chạy — bài toán dễ hơn hẳn "sinh JSON theo schema mình đoán là chạy".

Thứ tự ngược lại (làm meta-agent trước) rất dễ hấp dẫn vì nó cho cảm giác tiến triển nhanh, nhưng kết quả là một đống JSON đẹp mà không có gì thi hành được — và đến lúc viết runtime mới phát hiện schema cần đổi, kéo theo làm lại toàn bộ prompt của meta-agent.

### Hiện trạng repo

Template gần như trống: `src/agents/` chỉ có `example_node.py` và `example_tool.py` (calculator + search giả); `src/api/routes.py` có một route `/chat` mẫu sẽ bị thay; chưa có frontend; SQLAlchemy/psycopg2/Alembic còn comment trong `requirements.txt`; chưa có `CONTEXT.md`; `docs/adr/` rỗng.

### Rủi ro còn mở

- **Chưa chọn nơi deploy live URL** (Render / Railway / VPS), và chưa quyết Next.js + FastAPI + Postgres đặt cùng chỗ hay tách.
- **Độ tin cậy gọi tool của model rẻ** là chỗ thiết kế model-theo-bước dễ hỏng. Luật validate đã chặn trường hợp tệ nhất, nhưng allowlist cần được kiểm chứng bằng tay, không phải bằng suy đoán.
- **Judge của eval dao động** — đã có van an toàn (quyền vượt rào của admin, ngưỡng thấp cấu hình được), nhưng cần theo dõi xem ngưỡng mặc định có hợp lý.
- **Phủ trạng thái chạy thật trên sơ đồ** là mốc thứ hai của tính năng sơ đồ, không phải mốc đầu. Sơ đồ chỉ-đọc phải là một tính năng hoàn chỉnh tự đứng được trước, để nếu cháy thời gian thì vẫn còn thứ trọn vẹn để demo.

### Bốn quyết định đã cân nhắc và bị loại, ghi lại để không bị đề xuất lại

1. **Sinh code Python/LangGraph rồi `exec`** — rủi ro kỹ thuật cao nhất của đề tài, và làm mọi ràng buộc an toàn (kiểm soát tool, ước lượng chi phí, review khi duyệt) trở thành bất khả thi hoặc vô vọng.
2. **Duyệt agent một lần rồi cho sửa tự do** — lỗ bảo mật ngay trong thiết kế: được duyệt với tool vô hại rồi thêm tool rủi ro cao sau.
3. **Chỉ duyệt lại khi quyền leo thang** — *tin rằng* nó an toàn nhưng không phải, kiểu sai tệ nhất: thứ nguy hiểm nhất là **sửa prompt**, làm hành vi agent đổi hoàn toàn trong khi danh sách tool y nguyên.
4. **Langflow làm khung xem + sửa** — xem mục frontend.
