/* 页问 · buildless, browser-local interactive reading prototype. */
'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name, extra = '') => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHTML = (value) => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pad = (n) => String(n).padStart(2, '0');

const CONCEPTS = {
  alphaBeta: {name:'阿尔法与贝塔',subtitle:'超额收益背后，是哪一种来源？',description:'本页把阿尔法与策略价值联系起来，并在数值示例中假设贝塔为 1。延伸思考：比较两个基金时，如何避免把不同的市场暴露误认作不同的策略能力？',icon:'network'},
  attribution: {name:'收益归因',subtitle:'从「赚了多少」追问「为什么赚」',description:'本页阅读线索：将市场因素与策略价值分开。延伸练习：把「结果发生了」和「我知道结果的原因」写成两个判断，列出后者还需要什么证据。',icon:'network'},
  riskAdjusted: {name:'风险调整后的表现',subtitle:'比较收益之前，先比较承担的风险',description:'延伸思考，非本页原文：如果两个组合承担的市场风险不同，直接比较收益率可能遗漏什么？先识别比较条件，再思考怎样建立合适的基准。',icon:'focus'},
  skillLuck: {name:'能力与运气',subtitle:'一次好结果，能够说明多少？',description:'本页明确提醒，简单的阿尔法计算不能区分策略与好运。延伸思考：你愿意看到什么后续证据，才会提高对「策略有贡献」的信心？',icon:'bulb'},
  outOfSample: {name:'样本外检验',subtitle:'把结论带到没有参与选择的数据上',description:'延伸概念，非本页原文：先固定研究规则，再在未用于选择策略的数据上检验。它可以为检验策略提供思路，但不能保证未来表现，也不能一次排除所有偏差。',icon:'arrow'},
  falsification: {name:'可证伪性',subtitle:'什么结果，会让你修改原来的判断？',description:'延伸思考，非本页原文：在检验前先写下「什么现象会让我怀疑这个解释」。如果任何结果都被解读为支持策略，这个解释就很难接受真正的检验。',icon:'focus'},
  elaboration: {name:'精加工编码',subtitle:'把新知识，挂在已有的理解上',description:'理解一条新信息时，试着为它补上解释、例子，以及与已有经验的联系。这里的连接是一个阅读练习方向：你能给这句话找到一个自己的例子吗？',icon:'network'},
  transfer: {name:'知识迁移',subtitle:'换一个情境，理解还成立吗？',description:'先理解原文的条件，再尝试把这个理解用于另一个情境。注意保留必要的条件：表面相似的两个问题，未必有相同的解法。',icon:'arrow'},
  feynman: {name:'费曼学习法',subtitle:'用自己的话，让理解变得可见',description:'尝试向一个没有读过这本书的人解释这一段。遇到说不清楚的地方，先标下来，再回到原文寻找线索。把它当成检查理解的练习，而不是保证学会的捷径。',icon:'bulb'},
  retrieval: {name:'主动回忆',subtitle:'暂时合上书，还能想起什么？',description:'读完一个小节后，暂时不看原文，写下你记得的主张、理由和一个例子，再对照原文修正。想不起来的部分，可以成为下一次阅读的起点。',icon:'bulb'},
  boundary: {name:'概念边界',subtitle:'它成立的条件，在哪里？',description:'试着区分定义、例子和作者的判断。问一问：这个说法针对谁，在什么条件下成立，又排除了哪些情况？',icon:'focus'},
  causal: {name:'因果与相关',subtitle:'一起发生，是否意味着互为因果？',description:'寻找作者提出的机制和证据。还可能有什么第三个因素？如果去掉作者认为的原因，结果是否仍可能发生？这些问题能帮助你检查论证。',icon:'network'},
  counter: {name:'反例思考',subtitle:'找到一个让结论停下来的例子',description:'先尽量准确地重述作者的观点，再寻找一个可能不适用的情境。反例的价值，是帮助你缩小或澄清结论的范围。',icon:'focus'},
  feedback: {name:'反馈循环',subtitle:'结果，如何改变下一次行动？',description:'试着画出行动、结果、观察和调整之间的关系。哪个环节缺席时，这个过程就无法形成循环？再找一个你的真实经历来对照。',icon:'network'},
};

const SAMPLE_BOOK = {
  id:'quant-black-box-page55-v1', title:'打开量化投资的黑箱', author:'里什·纳兰 · 原书第 2 版', format:'PDF · 455 页', isDemo:true,
  sourceFile:'打开量化投资的黑箱.pdf', sourceImage:'assets/quant-page-55.png', sourcePage:55, totalPages:455,
  outline:[
    {number:1,title:'关注量化交易的原因',page:29},
    {number:2,title:'量化交易简介',page:43},
    {number:3,title:'阿尔法模型',page:55,active:true},
    {number:4,title:'风险模型',page:119},
    {number:5,title:'交易成本模型',page:137},
    {number:6,title:'投资组合构建模型',page:155},
    {number:7,title:'执行模型',page:189},
    {number:8,title:'数据',page:213},
    {number:9,title:'研究',page:234}
  ],
  pages:[{
    id:'pdf-page-55',title:'阿尔法模型：宽客如何盈利',short:'阿尔法模型',eyebrow:'第 3 章',
    subtitle:'第二部分 · 打开黑箱',minutes:4,
    epigraph:'预测，尤其是预测未来，是极为困难的一件事。',epigraphAuthor:'尼尔斯·玻尔',
    paragraphs:[
      '我们已经对黑箱的框架进行了研究，接下来将探索其内部，通过理解量化交易的核心交易系统来加深对黑箱操作的认识。阿尔法模型是量化交易系统的第一个重要组成部分，主要是为了寻找盈利机会。目前对量化交易的研究重点大都集中在对阿尔法模型的研究上。',
      '阿尔法是希腊字母α的音译，常用于量化表述投资者的盈利能力，或投资者得到的与市场波动无关的回报。[[0|在通常的定义中，阿尔法是指扣除市场基准回报之后的投资回报率，或仅仅是由投资策略所带来的价值。]]',
      '由于市场因素带来的回报率称为贝塔，例如，某基金的回报率是12%，而同时期的市场基准回报率是10%，则该基金的阿尔法值就是[[1|2%（这里假设了基金投资组合的贝塔恰好为1）]]。',
      '[[2|采用这种阿尔法计算方法的缺陷在于，无法判断收益是由交易策略还是单纯的好运带来的。]]显然，任何交易者都倾向于把超过基准回报率的利润归功于交易策略。',
      '阿尔法模型就是，为了增加盈利，在投资过程中所使用的一系列技巧或策略。例如，趋势跟随策略，就是系统地分析市场未来的走势，顺应市场趋势进行交易的一种策略。'
    ],
    highlights:[
      {
        quote:'在通常的定义中，阿尔法是指扣除市场基准回报之后的投资回报率，或仅仅是由投资策略所带来的价值。',kind:'core',
        question:'作者区分阿尔法与贝塔，最想让我们辨认什么？',description:'先做一个判断，再回到原文看看理由。',
        choices:['策略可能带来的价值，与市场因素带来的回报','扣除交易费用前的收益，与扣除费用后的收益','基金的正收益，与基金的负收益'],correct:0,
        explanation:'本页将阿尔法与策略带来的价值联系起来，将贝塔与市场因素带来的回报联系起来。这个区分关注收益从哪里来，而不是只看有没有赚钱。',
        followup:'如果一只基金只是承担了更多市场风险，跑赢基准就一定代表它有阿尔法吗？',
        hint:'对照原文中「投资策略所带来的价值」与下一段的「市场因素带来的回报」。',concepts:['alphaBeta','attribution','riskAdjusted']
      },
      {
        quote:'2%（这里假设了基金投资组合的贝塔恰好为1）',kind:'connect',
        question:'基金收益 12%、基准收益 10%。这页把阿尔法写成 2%，明确依赖什么假设？',description:'留意括号。一个短小的条件，可能决定结论的适用范围。',
        choices:['基金与基准的交易费用完全相同','基金投资组合的贝塔恰好为 1','基金与基准持有完全相同的证券'],correct:1,
        explanation:'原文明确写出「贝塔恰好为1」。因此这里的 12% − 10% = 2% 是带条件的示例，不能把这条简化计算不加区分地套在所有组合上。',
        followup:'把括号去掉，你会怎样误读这个 2%？读其他公式时，你会先寻找哪些前提？',
        hint:'计算结果后面的括号，直接给出了本题所需要的条件。',concepts:['alphaBeta','riskAdjusted','boundary']
      },
      {
        quote:'采用这种阿尔法计算方法的缺陷在于，无法判断收益是由交易策略还是单纯的好运带来的。',kind:'challenge',
        question:'看到一次跑赢基准的记录，哪一种追问最贴近作者在这里的提醒？',description:'从一个漂亮的结果，继续追问它背后的证据。',
        choices:['超额收益是否为正？只要为正，就足以归因于策略','策略是否复杂？模型越复杂，就越能排除好运','还需要什么证据，才能区分策略贡献与偶然波动？'],correct:2,
        explanation:'作者指出：这项计算本身无法区分策略与好运。一次超额收益可以成为继续研究的线索，但还不能单凭它完成能力归因。',
        followup:'你会怎样设计一次检验，给「这只是好运」这个解释一个公平的机会？',
        hint:'注意「无法判断」四个字。作者提醒的，是证据还没有支持哪一步推断。',concepts:['skillLuck','outOfSample','falsification']
      }
    ]
  }]
};
