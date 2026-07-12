import RecolorPanel from "../components/RecolorPanel";

export default function RecolorPage({
  onUseAsSource,
  onSendOriginalToAi
}: {
  onUseAsSource: (image: any) => void;
  onSendOriginalToAi: (image: any, targetColor: string) => void;
}) {
  return (
    <div className="page">
      <header className="page-header">
        <h1>智能调色</h1>
        <p>本地识别包包主体和五金保护区，快速调整包身、图案和花纹颜色。</p>
      </header>
      <RecolorPanel onUseAsSource={onUseAsSource} onSendOriginalToAi={onSendOriginalToAi} />
    </div>
  );
}
